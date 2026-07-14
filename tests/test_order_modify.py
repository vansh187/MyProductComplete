"""
Tests for ticket 15: order modify/amend endpoint (PUT /orders/{order_id}).

No real DB/network calls: OrderService, MarginEngine, and
WalletBalanceService are all mocked at the api/orders.py call boundary via
FastAPI's dependency override for get_current_user plus module-level patches.
"""

from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.orders import router
from utils.auth_dependency import get_current_user


def _make_client():
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: {"user_id": 42}
    return TestClient(app)


def _equity_buy_order(**overrides):
    order = {
        "id": 101, "user_id": 42, "symbol": "RELIANCE", "side": "BUY",
        "quantity": 10, "price": 2500.00, "status": "PENDING", "exchange": "NSE",
    }
    order.update(overrides)
    return order


class TestModifyOrderEndpoint:

    @patch("api.orders.MarginEngine")
    @patch("api.orders.WalletBalanceService")
    @patch("api.orders.OrderService")
    def test_no_fields_provided_is_rejected(self, MockOrderService, MockWallet, MockMargin):
        client = _make_client()
        resp = client.put("/orders/101", json={})
        assert resp.status_code == 400
        MockOrderService.return_value.modify_order_by_id.assert_not_called()

    @patch("api.orders.MarginEngine")
    @patch("api.orders.WalletBalanceService")
    @patch("api.orders.OrderService")
    def test_order_not_found_returns_404(self, MockOrderService, MockWallet, MockMargin):
        MockOrderService.return_value.get_order_by_id.return_value = None
        client = _make_client()
        resp = client.put("/orders/101", json={"price": 2600.0})
        assert resp.status_code == 404

    @patch("api.orders.MarginEngine")
    @patch("api.orders.WalletBalanceService")
    @patch("api.orders.OrderService")
    def test_already_executed_order_cannot_be_modified(self, MockOrderService, MockWallet, MockMargin):
        MockOrderService.return_value.get_order_by_id.return_value = _equity_buy_order(status="EXECUTED")
        client = _make_client()
        resp = client.put("/orders/101", json={"price": 2600.0})
        assert resp.status_code == 400
        assert "pending" in resp.json()["detail"].lower()

    @patch("api.orders.MarginEngine")
    @patch("api.orders.WalletBalanceService")
    @patch("api.orders.OrderService")
    def test_margin_required_order_is_rejected(self, MockOrderService, MockWallet, MockMargin):
        """OPTION SELL / FUTURES orders are out of scope for modify - must be
        rejected outright, not half-supported (see modify_order_by_id's
        docstring for why: no atomic release-then-reblock exists yet)."""
        MockOrderService.return_value.get_order_by_id.return_value = _equity_buy_order(
            side="SELL", symbol="NIFTY14JUL2623950CE", exchange="NFO",
        )
        MockMargin.return_value.resolve_contract_type.return_value = {"contract_type": "OPTION"}
        MockMargin.return_value.is_margin_required.return_value = True

        client = _make_client()
        resp = client.put("/orders/101", json={"price": 120.0})

        assert resp.status_code == 400
        assert "margin" in resp.json()["detail"].lower()
        MockWallet.return_value.debitWalletIfSufficient.assert_not_called()

    @patch("api.orders.MarginEngine")
    @patch("api.orders.WalletBalanceService")
    @patch("api.orders.OrderService")
    def test_increasing_buy_order_value_debits_the_delta(self, MockOrderService, MockWallet, MockMargin):
        """price 2500->2600, qty unchanged (10): delta = 1000."""
        MockOrderService.return_value.get_order_by_id.return_value = _equity_buy_order()
        MockMargin.return_value.resolve_contract_type.return_value = {"contract_type": None}
        MockMargin.return_value.is_margin_required.return_value = False
        MockWallet.return_value.debitWalletIfSufficient.return_value = True
        MockOrderService.return_value.modify_order_by_id.return_value = {"modified": True}

        client = _make_client()
        resp = client.put("/orders/101", json={"price": 2600.0})

        assert resp.status_code == 200
        MockWallet.return_value.debitWalletIfSufficient.assert_called_once()
        _, delta = MockWallet.return_value.debitWalletIfSufficient.call_args.args
        assert Decimal(str(delta)) == Decimal("1000.00")
        MockWallet.return_value.creditWalletStandalone.assert_not_called()

    @patch("api.orders.MarginEngine")
    @patch("api.orders.WalletBalanceService")
    @patch("api.orders.OrderService")
    def test_decreasing_buy_order_value_refunds_the_delta(self, MockOrderService, MockWallet, MockMargin):
        """price 2500->2400, qty unchanged (10): refund 1000."""
        MockOrderService.return_value.get_order_by_id.return_value = _equity_buy_order()
        MockMargin.return_value.resolve_contract_type.return_value = {"contract_type": None}
        MockMargin.return_value.is_margin_required.return_value = False
        MockOrderService.return_value.modify_order_by_id.return_value = {"modified": True}

        client = _make_client()
        resp = client.put("/orders/101", json={"price": 2400.0})

        assert resp.status_code == 200
        MockWallet.return_value.creditWalletStandalone.assert_called_once()
        _, refund = MockWallet.return_value.creditWalletStandalone.call_args.args
        assert Decimal(str(refund)) == Decimal("1000.00")
        MockWallet.return_value.debitWalletIfSufficient.assert_not_called()

    @patch("api.orders.MarginEngine")
    @patch("api.orders.WalletBalanceService")
    @patch("api.orders.OrderService")
    def test_insufficient_balance_for_increased_value_is_rejected(self, MockOrderService, MockWallet, MockMargin):
        MockOrderService.return_value.get_order_by_id.return_value = _equity_buy_order()
        MockMargin.return_value.resolve_contract_type.return_value = {"contract_type": None}
        MockMargin.return_value.is_margin_required.return_value = False
        MockWallet.return_value.debitWalletIfSufficient.return_value = False

        client = _make_client()
        resp = client.put("/orders/101", json={"price": 100000.0})

        assert resp.status_code == 400
        assert "insufficient" in resp.json()["detail"].lower()
        MockOrderService.return_value.modify_order_by_id.assert_not_called()

    @patch("api.orders.MarginEngine")
    @patch("api.orders.WalletBalanceService")
    @patch("api.orders.OrderService")
    def test_lost_race_reverses_wallet_delta_and_returns_409(self, MockOrderService, MockWallet, MockMargin):
        """The order got matched/cancelled between the status check and the
        actual write - the wallet delta already applied must be reversed,
        and the caller told the modify didn't happen."""
        MockOrderService.return_value.get_order_by_id.return_value = _equity_buy_order()
        MockMargin.return_value.resolve_contract_type.return_value = {"contract_type": None}
        MockMargin.return_value.is_margin_required.return_value = False
        MockWallet.return_value.debitWalletIfSufficient.return_value = True
        MockOrderService.return_value.modify_order_by_id.return_value = {"modified": False}

        client = _make_client()
        resp = client.put("/orders/101", json={"price": 2600.0})

        assert resp.status_code == 409
        # Debited 1000 going in, must be credited back 1000 on the race loss.
        MockWallet.return_value.creditWalletStandalone.assert_called_once()
        _, reversed_amount = MockWallet.return_value.creditWalletStandalone.call_args.args
        assert Decimal(str(reversed_amount)) == Decimal("1000.00")

    @patch("api.orders.MarginEngine")
    @patch("api.orders.WalletBalanceService")
    @patch("api.orders.OrderService")
    def test_equity_sell_order_never_touches_wallet(self, MockOrderService, MockWallet, MockMargin):
        """SELL orders never cash-debit at creation (holdings-based) - modify
        must not touch the wallet for them either."""
        MockOrderService.return_value.get_order_by_id.return_value = _equity_buy_order(side="SELL")
        MockMargin.return_value.resolve_contract_type.return_value = {"contract_type": None}
        MockMargin.return_value.is_margin_required.return_value = False
        MockOrderService.return_value.modify_order_by_id.return_value = {"modified": True}

        client = _make_client()
        resp = client.put("/orders/101", json={"price": 2600.0})

        assert resp.status_code == 200
        MockWallet.return_value.debitWalletIfSufficient.assert_not_called()
        MockWallet.return_value.creditWalletStandalone.assert_not_called()

    @patch("api.orders.MarginEngine")
    @patch("api.orders.WalletBalanceService")
    @patch("api.orders.OrderService")
    def test_quantity_only_change_recomputes_required_balance(self, MockOrderService, MockWallet, MockMargin):
        """qty 10->15, price unchanged (2500): delta = 5*2500 = 12500."""
        MockOrderService.return_value.get_order_by_id.return_value = _equity_buy_order()
        MockMargin.return_value.resolve_contract_type.return_value = {"contract_type": None}
        MockMargin.return_value.is_margin_required.return_value = False
        MockWallet.return_value.debitWalletIfSufficient.return_value = True
        MockOrderService.return_value.modify_order_by_id.return_value = {"modified": True}

        client = _make_client()
        resp = client.put("/orders/101", json={"quantity": 15})

        assert resp.status_code == 200
        _, delta = MockWallet.return_value.debitWalletIfSufficient.call_args.args
        assert Decimal(str(delta)) == Decimal("12500.00")

    @patch("api.orders.MarginEngine")
    @patch("api.orders.WalletBalanceService")
    @patch("api.orders.OrderService")
    def test_live_broker_order_cannot_be_modified(self, MockOrderService, MockWallet, MockMargin):
        """An order already routed to the real Shoonya broker
        (broker_order_id set) must be blocked outright - modifying only the
        internal row here would desync from the real resting order, which
        keeps filling on its original, unmodified terms at the exchange."""
        MockOrderService.return_value.get_order_by_id.return_value = _equity_buy_order(
            symbol="NIFTY14JUL2623950CE", exchange="NFO", broker_order_id="20052000000017",
        )
        client = _make_client()
        resp = client.put("/orders/101", json={"price": 105.0})

        assert resp.status_code == 400
        assert "broker" in resp.json()["detail"].lower()
        MockOrderService.return_value.modify_order_by_id.assert_not_called()
        MockWallet.return_value.debitWalletIfSufficient.assert_not_called()

    def test_negative_or_zero_price_is_rejected_by_pydantic(self):
        client = _make_client()
        resp = client.put("/orders/101", json={"price": 0})
        assert resp.status_code == 422
        resp = client.put("/orders/101", json={"quantity": -5})
        assert resp.status_code == 422


class TestCancelOrderLiveGuard:

    @patch("api.orders.OrderService")
    def test_live_broker_order_cannot_be_cancelled(self, MockOrderService):
        """Same reasoning as the modify guard above: cancelling only the
        internal row for an order already routed to the real broker would
        refund the wallet/release margin while the real order stays resting
        and can still fill for real later - an unfunded real position."""
        MockOrderService.return_value.get_order_by_id.return_value = _equity_buy_order(
            symbol="NIFTY14JUL2623950CE", exchange="NFO", broker_order_id="20052000000017",
        )
        client = _make_client()
        resp = client.post("/orders/101/cancel")

        assert resp.status_code == 400
        assert "broker" in resp.json()["detail"].lower()
        MockOrderService.return_value.cancel_order_by_id.assert_not_called()

    @patch("api.orders.OrderService")
    def test_non_live_order_cancels_normally(self, MockOrderService):
        MockOrderService.return_value.get_order_by_id.return_value = _equity_buy_order()
        MockOrderService.return_value.cancel_order_by_id.return_value = True
        client = _make_client()
        resp = client.post("/orders/101/cancel")

        assert resp.status_code == 200
        MockOrderService.return_value.cancel_order_by_id.assert_called_once_with(42, 101)

    @patch("api.orders.MarginEngine")
    @patch("api.orders.WalletBalanceService")
    @patch("api.orders.OrderService")
    def test_trigger_price_only_change_passes_expected_trigger_price_for_cas(self, MockOrderService, MockWallet, MockMargin):
        """Code review caught that the optimistic-concurrency guard
        previously covered price/quantity but not trigger_price, so two
        concurrent trigger_price-only modify requests weren't mutually
        excluded - this proves the API now reads the existing trigger_price
        and passes it through as expected_trigger_price so the persistence
        layer's CAS can catch that race too."""
        MockOrderService.return_value.get_order_by_id.return_value = _equity_buy_order(
            side="SELL", trigger_price=95.0,
        )
        MockMargin.return_value.resolve_contract_type.return_value = {"contract_type": None}
        MockMargin.return_value.is_margin_required.return_value = False
        MockOrderService.return_value.modify_order_by_id.return_value = {"modified": True}

        client = _make_client()
        resp = client.put("/orders/101", json={"trigger_price": 96.0})

        assert resp.status_code == 200
        call_kwargs = MockOrderService.return_value.modify_order_by_id.call_args.kwargs
        assert call_kwargs["expected_trigger_price"] == 95.0
        assert call_kwargs["trigger_price"] == 96.0
