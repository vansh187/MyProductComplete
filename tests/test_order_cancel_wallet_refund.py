"""
Unit tests for OrderService.cancel_order_by_id()'s wallet refund on
successful cancellation.

Previously nothing refunded the cash debited at order-creation time for a
BUY order (api/orders.py's atomic debitWalletIfSufficient) when that order
was later cancelled before it filled - margin_engine.release_on_cancel()
only releases F&O margin blocks, a separate subsystem. A user could place
a BUY LIMIT order, cancel it unfilled, and permanently lose that cash with
no refund.

No real DB/network calls are made: OrderPersistence, MarginEngine, and
WalletBalanceService are all mocked.
"""

from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from service.orderService import OrderService


def _make_service(order_details=None, cancel_returns=True):
    service = OrderService()
    service.order_persistence = MagicMock()
    service.order_persistence.get_order_by_id.return_value = order_details
    service.order_persistence.cancel_order_by_id.return_value = cancel_returns
    service.margin_engine = MagicMock()
    service.wallet_service = MagicMock()
    return service


def _equity_buy_order(**overrides):
    order = {
        "id": 101, "user_id": 42, "symbol": "RELIANCE", "side": "BUY",
        "quantity": 10, "price": 2500.00, "status": "PENDING", "exchange": "NSE",
    }
    order.update(overrides)
    return order


class TestCancelRefundsBuyOrder:

    def test_cancelled_buy_order_refunds_full_quantity_times_price(self):
        service = _make_service(order_details=_equity_buy_order())
        service.margin_engine.resolve_contract_type.return_value = {"contract_type": None}

        result = service.cancel_order_by_id(42, 101)

        assert result is True
        service.wallet_service.creditWalletStandalone.assert_called_once()
        refund_user_id, refund_amount = service.wallet_service.creditWalletStandalone.call_args.args
        assert refund_user_id == 42
        assert Decimal(str(refund_amount)) == Decimal("25000.00")

    def test_cancelled_sell_order_is_never_refunded(self):
        """SELL orders never debit cash at creation - nothing to refund."""
        service = _make_service(order_details=_equity_buy_order(side="SELL"))
        service.margin_engine.resolve_contract_type.return_value = {"contract_type": None}

        service.cancel_order_by_id(42, 101)

        service.wallet_service.creditWalletStandalone.assert_not_called()

    def test_cancelled_futures_buy_order_is_never_refunded(self):
        """FUTURES BUY was never cash-debited at creation (margin-based
        instead) - margin_engine.release_on_cancel() already covers it."""
        service = _make_service(order_details=_equity_buy_order(symbol="NIFTY26JUL26FUT"))
        service.margin_engine.resolve_contract_type.return_value = {"contract_type": "FUTURES"}

        service.cancel_order_by_id(42, 101)

        service.wallet_service.creditWalletStandalone.assert_not_called()

    def test_option_buy_order_is_refunded(self):
        """OPTION BUY (opening or closing a short) does cash-debit at
        creation, same as equity BUY - must be refunded on cancel."""
        service = _make_service(order_details=_equity_buy_order(
            symbol="NIFTY14JUL2623950CE", price=101.15, quantity=75, exchange="NFO",
        ))
        service.margin_engine.resolve_contract_type.return_value = {"contract_type": "OPTION"}

        service.cancel_order_by_id(42, 101)

        service.wallet_service.creditWalletStandalone.assert_called_once()
        _, refund_amount = service.wallet_service.creditWalletStandalone.call_args.args
        assert Decimal(str(refund_amount)) == Decimal("7586.25")

    def test_no_pending_order_to_cancel_never_refunds(self):
        """cancel_order_by_id returning False (already filled/cancelled/
        doesn't exist) must never trigger a refund."""
        service = _make_service(order_details=_equity_buy_order(), cancel_returns=False)
        service.margin_engine.resolve_contract_type.return_value = {"contract_type": None}

        result = service.cancel_order_by_id(42, 101)

        assert result is False
        service.wallet_service.creditWalletStandalone.assert_not_called()

    def test_missing_order_details_does_not_crash_despite_successful_cancel(self):
        """Defensive edge case: if order_details somehow came back None
        despite the cancel succeeding, must log and return cleanly, not
        raise (the cancel response to the user must not be broken by a
        refund-lookup problem)."""
        service = _make_service(order_details=None, cancel_returns=True)

        result = service.cancel_order_by_id(42, 101)

        assert result is True
        service.wallet_service.creditWalletStandalone.assert_not_called()

    def test_refund_db_error_is_logged_not_raised(self):
        """A DB error during the refund itself must not propagate - the
        cancellation has already committed by this point, so there's
        nothing left to roll back; this must be logged loudly for manual
        reconciliation, not silently swallowed and not crash the cancel
        response."""
        service = _make_service(order_details=_equity_buy_order())
        service.margin_engine.resolve_contract_type.return_value = {"contract_type": None}
        service.wallet_service.creditWalletStandalone.side_effect = Exception("connection reset by peer")

        result = service.cancel_order_by_id(42, 101)  # must not raise

        assert result is True

    def test_margin_release_failure_does_not_block_wallet_refund(self):
        """margin_engine.release_on_cancel() raising must not prevent the
        (independent) wallet refund from still running."""
        from service.marginengine.exceptions import MarginEngineError

        service = _make_service(order_details=_equity_buy_order())
        service.margin_engine.release_on_cancel.side_effect = MarginEngineError("margin release blew up")
        service.margin_engine.resolve_contract_type.return_value = {"contract_type": None}

        result = service.cancel_order_by_id(42, 101)

        assert result is True
        service.wallet_service.creditWalletStandalone.assert_called_once()

    def test_zero_or_negative_price_never_refunded(self):
        """Defensive guard: an order with no valid price stored (shouldn't
        happen given api/orders.py validates this at creation, but must
        not crash or refund a bogus amount if it ever does)."""
        service = _make_service(order_details=_equity_buy_order(price=0))
        service.margin_engine.resolve_contract_type.return_value = {"contract_type": None}

        service.cancel_order_by_id(42, 101)

        service.wallet_service.creditWalletStandalone.assert_not_called()
