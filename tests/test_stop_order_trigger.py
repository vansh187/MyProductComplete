"""
Tests for ticket 14: STOP/STOP-LIMIT orders don't actually trigger.

Covers:
  - StopOrderTriggerService._trigger_condition_met (pure decision logic,
    every side/order_type/price combination)
  - StopOrderTriggerService.check_and_trigger's DB-facing orchestration
    (mocked persistence/connection - no real DB)
  - database/portfolioPersistence.py.createTradeinOrderBook's dormant-status
    behavior for STOP/STOPLIMIT orders (previously always inserted as an
    immediately-matchable 'LIMIT' order regardless of order_type - the core
    bug this ticket fixes)
  - ExecutionEngine.execute_order's own creation-time matching pass: a
    CRITICAL regression caught by code review - the dormant-status fix above
    only protects a STOP/STOPLIMIT order from OTHER orders' matching passes;
    without a separate guard in execute_order itself, the order's OWN
    creation-time Phase 3 matching would still match it immediately against
    the resting book, reproducing the exact "executes the instant it's
    placed" bug this whole feature exists to fix.

No real DB/network calls are made anywhere in this file.
"""

from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from service.stopOrderTriggerService import StopOrderTriggerService


def _resting_row(**overrides):
    row = {
        "id": 501, "user_id": 7, "order_id": 900, "symbol": "RELIANCE",
        "side": "SELL", "order_type": "STOP", "quantity": 10,
        "remaining_quantity": 10, "price": None, "trigger_price": Decimal("2400.00"),
        "exchange": "NSE", "product_type": "MIS", "validity": "DAY",
        "client_order_id": None,
    }
    row.update(overrides)
    return row


class TestTriggerConditionMet:

    def test_sell_stop_triggers_when_ltp_falls_to_trigger_price(self):
        row = _resting_row(side="SELL", trigger_price=Decimal("2400.00"))
        result = StopOrderTriggerService._trigger_condition_met(row, Decimal("2400.00"))
        assert result == Decimal("2400.00")

    def test_sell_stop_triggers_when_ltp_falls_below_trigger_price(self):
        row = _resting_row(side="SELL", trigger_price=Decimal("2400.00"))
        result = StopOrderTriggerService._trigger_condition_met(row, Decimal("2390.00"))
        assert result == Decimal("2390.00")

    def test_sell_stop_does_not_trigger_while_ltp_stays_above_trigger(self):
        row = _resting_row(side="SELL", trigger_price=Decimal("2400.00"))
        result = StopOrderTriggerService._trigger_condition_met(row, Decimal("2401.00"))
        assert result is None

    def test_buy_stop_triggers_when_ltp_rises_to_trigger_price(self):
        row = _resting_row(side="BUY", trigger_price=Decimal("2500.00"))
        result = StopOrderTriggerService._trigger_condition_met(row, Decimal("2500.00"))
        assert result == Decimal("2500.00")

    def test_buy_stop_triggers_when_ltp_rises_above_trigger(self):
        row = _resting_row(side="BUY", trigger_price=Decimal("2500.00"))
        result = StopOrderTriggerService._trigger_condition_met(row, Decimal("2510.00"))
        assert result == Decimal("2510.00")

    def test_buy_stop_does_not_trigger_while_ltp_stays_below_trigger(self):
        row = _resting_row(side="BUY", trigger_price=Decimal("2500.00"))
        result = StopOrderTriggerService._trigger_condition_met(row, Decimal("2499.99"))
        assert result is None

    def test_stoplimit_keeps_its_own_limit_price_once_triggered(self):
        """A STOPLIMIT order must execute-attempt at its OWN submitted limit
        price, not the triggering ltp - this is what distinguishes it from a
        plain STOP order."""
        row = _resting_row(
            side="SELL", order_type="STOPLIMIT",
            price=Decimal("2395.00"), trigger_price=Decimal("2400.00"),
        )
        result = StopOrderTriggerService._trigger_condition_met(row, Decimal("2390.00"))
        assert result == Decimal("2395.00")

    def test_plain_stop_adopts_the_triggering_ltp_as_its_price(self):
        """A plain STOP order has no limit price of its own - it becomes
        marketable at whatever price actually triggered it."""
        row = _resting_row(side="SELL", order_type="STOP", price=None, trigger_price=Decimal("2400.00"))
        result = StopOrderTriggerService._trigger_condition_met(row, Decimal("2390.00"))
        assert result == Decimal("2390.00")

    def test_missing_trigger_price_never_triggers(self):
        row = _resting_row(trigger_price=None)
        result = StopOrderTriggerService._trigger_condition_met(row, Decimal("2400.00"))
        assert result is None

    def test_unrecognized_side_never_triggers(self):
        row = _resting_row(side="SHORT")
        result = StopOrderTriggerService._trigger_condition_met(row, Decimal("2400.00"))
        assert result is None


class TestCheckAndTrigger:

    def _service_with_mocked_db(self):
        service = StopOrderTriggerService()
        return service

    def test_no_symbol_or_no_ltp_is_a_safe_no_op(self):
        service = self._service_with_mocked_db()
        service.check_and_trigger("", 100)  # must not raise
        service.check_and_trigger("RELIANCE", None)  # must not raise

    def test_non_numeric_ltp_is_a_safe_no_op(self):
        service = self._service_with_mocked_db()
        service.check_and_trigger("RELIANCE", "not-a-number")  # must not raise

    @patch("service.stopOrderTriggerService.PostgresConnectionFactory")
    @patch("service.stopOrderTriggerService.portfolioPersistence")
    def test_triggered_order_is_flipped_to_pending_and_matching_is_invoked(self, mock_persistence, mock_conn_factory):
        mock_conn = MagicMock()
        mock_conn_factory.create_connection.return_value = mock_conn
        mock_persistence.get_pending_trigger_orders_by_symbol.return_value = [
            _resting_row(side="SELL", trigger_price=Decimal("2400.00"))
        ]

        service = StopOrderTriggerService()
        with patch.object(service, "_run_matching_for_triggered_order") as mock_run_matching:
            service.check_and_trigger("RELIANCE", Decimal("2390.00"))

        mock_persistence.mark_order_book_triggered.assert_called_once()
        mock_persistence.mark_orders_triggered.assert_called_once()
        mock_conn.commit.assert_called_once()
        mock_run_matching.assert_called_once()
        triggered_arg = mock_run_matching.call_args.args[0]
        assert triggered_arg["order_id"] == 900
        assert triggered_arg["price"] == Decimal("2390.00")

    @patch("service.stopOrderTriggerService.PostgresConnectionFactory")
    @patch("service.stopOrderTriggerService.portfolioPersistence")
    def test_untriggered_order_is_left_dormant(self, mock_persistence, mock_conn_factory):
        mock_conn = MagicMock()
        mock_conn_factory.create_connection.return_value = mock_conn
        mock_persistence.get_pending_trigger_orders_by_symbol.return_value = [
            _resting_row(side="SELL", trigger_price=Decimal("2400.00"))
        ]

        service = StopOrderTriggerService()
        with patch.object(service, "_run_matching_for_triggered_order") as mock_run_matching:
            service.check_and_trigger("RELIANCE", Decimal("2450.00"))  # above trigger, SELL stop shouldn't fire

        mock_persistence.mark_order_book_triggered.assert_not_called()
        mock_persistence.mark_orders_triggered.assert_not_called()
        mock_run_matching.assert_not_called()

    @patch("service.stopOrderTriggerService.PostgresConnectionFactory")
    @patch("service.stopOrderTriggerService.portfolioPersistence")
    def test_db_failure_rolls_back_and_never_raises(self, mock_persistence, mock_conn_factory):
        mock_conn = MagicMock()
        mock_conn_factory.create_connection.return_value = mock_conn
        mock_persistence.get_pending_trigger_orders_by_symbol.side_effect = Exception("connection reset")

        service = StopOrderTriggerService()
        service.check_and_trigger("RELIANCE", Decimal("2390.00"))  # must not raise

        mock_conn.rollback.assert_called_once()

    @patch("service.stopOrderTriggerService.PostgresConnectionFactory")
    @patch("service.stopOrderTriggerService.portfolioPersistence")
    def test_matching_failure_for_one_triggered_order_does_not_raise(self, mock_persistence, mock_conn_factory):
        """A failure running matching for a triggered order must be
        contained - it must not propagate up into the caller (a live trade
        commit or a live tick), which would be a severe regression."""
        mock_conn = MagicMock()
        mock_conn_factory.create_connection.return_value = mock_conn
        mock_persistence.get_pending_trigger_orders_by_symbol.return_value = [
            _resting_row(side="SELL", trigger_price=Decimal("2400.00"))
        ]

        service = StopOrderTriggerService()
        with patch.object(service, "_run_matching_for_triggered_order", side_effect=Exception("boom")):
            service.check_and_trigger("RELIANCE", Decimal("2390.00"))  # must not raise


class TestCreateTradeInOrderBookDormantStatus:
    """Regression coverage for the core bug: STOP/STOPLIMIT orders were
    previously inserted as immediately-matchable 'LIMIT' orders regardless of
    their real order_type, so they executed instantly instead of waiting for
    trigger_price to be crossed."""

    def _make_order(self, order_type, price=None, trigger_price=None):
        order = MagicMock()
        order.symbol = "RELIANCE"
        order.side = MagicMock(value="SELL")
        order.order_type = MagicMock(value=order_type)
        order.quantity = 10
        order.price = price
        order.trigger_price = trigger_price
        order.exchange = MagicMock(value="NSE")
        order.product_type = MagicMock(value="MIS")
        order.validity = MagicMock(value="DAY")
        order.client_order_id = None
        return order

    def _run_and_capture_status(self, order):
        from database.portfolioPersistence import portfolioPersistence

        cursor = MagicMock()
        cursor.fetchone.return_value = {"id": 1}
        with patch("database.portfolioPersistence.QueryLoader") as mock_loader:
            mock_loader.get.return_value = "INSERT ..."
            portfolioPersistence().createTradeinOrderBook(MagicMock(), cursor, order, 7, 900)
        params = cursor.execute.call_args.args[1]
        return params

    def test_stop_order_is_created_dormant(self):
        order = self._make_order("STOP", price=None, trigger_price=2400.0)
        params = self._run_and_capture_status(order)
        # params order: user_id, symbol, side, order_type, quantity,
        # remaining_qty, price, trigger_price, exchange, product_type,
        # validity, client_order_id, status, order_id
        assert params[3] == "STOP"
        assert params[-2] == "PENDING_TRIGGER"

    def test_stoplimit_order_is_created_dormant(self):
        order = self._make_order("STOPLIMIT", price=2395.0, trigger_price=2400.0)
        params = self._run_and_capture_status(order)
        assert params[3] == "STOPLIMIT"
        assert params[-2] == "PENDING_TRIGGER"

    def test_limit_order_is_still_immediately_matchable(self):
        order = self._make_order("LIMIT", price=2500.0, trigger_price=None)
        params = self._run_and_capture_status(order)
        assert params[3] == "LIMIT"
        assert params[-2] == "PENDING"

    def test_market_order_is_still_immediately_matchable(self):
        order = self._make_order("MARKET", price=None, trigger_price=None)
        params = self._run_and_capture_status(order)
        assert params[3] == "MARKET"
        assert params[-2] == "PENDING"


class TestExecuteOrderSkipsMatchingForDormantOrders:
    """CRITICAL regression coverage: a freshly-created STOP/STOPLIMIT order
    must not be matched against the resting book during its OWN creation-time
    execute_order() call - only a later trigger (StopOrderTriggerService)
    may make it matchable."""

    def _make_order_create(self, order_type):
        order = MagicMock()
        order.symbol = "RELIANCE"
        order.side = MagicMock(value="SELL")
        order.order_type = MagicMock(value=order_type)
        order.quantity = 10
        order.price = None
        order.trigger_price = 2400.0
        order.exchange = MagicMock(value="NSE")
        order.product_type = MagicMock(value="MIS")
        order.validity = MagicMock(value="DAY")
        order.client_order_id = None
        return order

    @patch("service.executionEngine.PostgresConnectionFactory")
    @patch("service.executionEngine.portfolioService")
    def test_stop_order_creation_never_calls_matching(self, mock_portfolio_service, mock_conn_factory):
        from service.executionEngine import ExecutionEngine

        mock_conn = MagicMock()
        mock_conn_factory.create_connection.return_value = mock_conn
        mock_portfolio_service.return_value.createTradeinOrderBook.return_value = 501

        order = self._make_order_create("STOP")
        engine = ExecutionEngine(order, 900)
        with patch.object(engine, "_execute_matching") as mock_matching:
            result = engine.execute_order(7)

        mock_matching.assert_not_called()
        assert result["status"] == "PENDING_TRIGGER"

    @patch("service.executionEngine.PostgresConnectionFactory")
    @patch("service.executionEngine.portfolioService")
    def test_stoplimit_order_creation_never_calls_matching(self, mock_portfolio_service, mock_conn_factory):
        from service.executionEngine import ExecutionEngine

        mock_conn = MagicMock()
        mock_conn_factory.create_connection.return_value = mock_conn
        mock_portfolio_service.return_value.createTradeinOrderBook.return_value = 501

        order = self._make_order_create("STOPLIMIT")
        engine = ExecutionEngine(order, 900)
        with patch.object(engine, "_execute_matching") as mock_matching:
            result = engine.execute_order(7)

        mock_matching.assert_not_called()
        assert result["status"] == "PENDING_TRIGGER"

    @patch("service.executionEngine.PostgresConnectionFactory")
    @patch("service.executionEngine.portfolioService")
    def test_limit_order_creation_still_calls_matching(self, mock_portfolio_service, mock_conn_factory):
        """Control case: MARKET/LIMIT orders must be unaffected and still
        proceed to immediate matching at creation."""
        from service.executionEngine import ExecutionEngine

        mock_conn = MagicMock()
        mock_conn_factory.create_connection.return_value = mock_conn
        mock_portfolio_service.return_value.createTradeinOrderBook.return_value = 501

        order = self._make_order_create("LIMIT")
        order.price = 2500.0
        engine = ExecutionEngine(order, 900)
        with patch.object(engine, "_execute_matching", return_value={"success": True, "status": "PENDING"}) as mock_matching:
            engine.execute_order(7)

        mock_matching.assert_called_once()

    @patch("service.executionEngine.PostgresConnectionFactory")
    @patch("service.executionEngine.portfolioService")
    def test_triggered_stop_order_proceeds_to_matching(self, mock_portfolio_service, mock_conn_factory):
        """When existing_order_book_id is provided (StopOrderTriggerService
        has already flipped the row to PENDING), matching must run - this is
        how a triggered STOP order actually gets matched."""
        from service.executionEngine import ExecutionEngine

        mock_conn = MagicMock()
        mock_conn_factory.create_connection.return_value = mock_conn

        order = self._make_order_create("STOP")
        order.price = 2390.0
        engine = ExecutionEngine(order, 900)
        with patch.object(engine, "_execute_matching", return_value={"success": True, "status": "PENDING"}) as mock_matching:
            engine.execute_order(7, existing_order_book_id=501)

        mock_matching.assert_called_once()
        mock_portfolio_service.return_value.createTradeinOrderBook.assert_not_called()
