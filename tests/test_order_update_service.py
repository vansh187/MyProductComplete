"""
Tests for service/orderUpdateService.py - reconciles a real Shoonya order
update (fill/reject/cancel, pushed over the shared price-tick WebSocket) back
into positions/wallet/order status.

No real DB/network calls: OrderPersistence, OrderService,
TradeSettlementService, and TradeHistoryService are all mocked. Also mocks
PostgresConnectionFactory so _handle_fill's own connection/cursor never
touches a real database.
"""

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from service.orderUpdateService import OrderUpdateService


def _order_row(**overrides):
    row = {
        "id": 101, "user_id": 42, "symbol": "NIFTY14JUL2623950CE", "side": "BUY",
        "quantity": 75, "price": 101.15, "status": "PENDING", "exchange": "NFO",
        "order_type": "LIMIT", "product_type": "MIS", "validity": "DAY",
        "trigger_price": None, "client_order_id": None, "broker_order_id": "20052000000017",
        "lot_size": 75, "token": "12345",
    }
    row.update(overrides)
    return row


def _run(coro):
    return asyncio.run(coro)


class TestHandleOrderUpdateDispatch:

    def test_missing_norenordno_is_a_safe_no_op(self):
        service = OrderUpdateService()
        service.order_persistence = MagicMock()
        _run(service.handle_order_update({"reporttype": "Fill"}))
        service.order_persistence.get_order_by_broker_order_id.assert_not_called()

    def test_missing_reporttype_is_a_safe_no_op(self):
        """Ack-only messages (e.g. subscription 'ok') have no reporttype."""
        service = OrderUpdateService()
        service.order_persistence = MagicMock()
        _run(service.handle_order_update({"norenordno": "123"}))
        service.order_persistence.get_order_by_broker_order_id.assert_not_called()

    def test_unknown_broker_order_id_is_logged_and_ignored(self):
        service = OrderUpdateService()
        service.order_persistence = MagicMock()
        service.order_persistence.get_order_by_broker_order_id.return_value = None
        _run(service.handle_order_update({"norenordno": "unknown", "reporttype": "Fill"}))  # must not raise

    def test_exception_in_handler_never_propagates(self):
        """This runs on a hot path fed by the broker's own WS thread - a bad
        update must never crash processing of the next one."""
        service = OrderUpdateService()
        service.order_persistence = MagicMock()
        service.order_persistence.get_order_by_broker_order_id.side_effect = Exception("db exploded")
        _run(service.handle_order_update({"norenordno": "123", "reporttype": "Fill"}))  # must not raise


class TestRemarksFallbackForRacingFills:
    """Covers the race where an update arrives before place_live_order's own
    set_broker_order_id write has committed - the broker's own `remarks` tag
    (stamped at placement as f"primepip_{order_id}") is the fallback lookup
    so the fill isn't silently dropped forever."""

    def _service(self):
        service = OrderUpdateService()
        service.order_persistence = MagicMock()
        service.order_service = MagicMock()
        service.trade_settlement_service = MagicMock()
        service.trade_history_service = MagicMock()
        service.trade_history_service.getFillStats.return_value = (101.15, 75)
        return service

    @patch("service.orderUpdateService.PostgresConnectionFactory")
    def test_unresolved_broker_order_id_falls_back_to_remarks_and_backfills(self, mock_conn_factory):
        service = self._service()
        service.order_persistence.get_order_by_broker_order_id.return_value = None
        service.order_persistence.get_order_by_id_only.return_value = _order_row(broker_order_id=None)
        mock_conn_factory.create_connection.return_value = MagicMock()

        _run(service.handle_order_update({
            "norenordno": "20052000000017", "reporttype": "Fill",
            "flqty": "75", "flprc": "101.15", "remarks": "primepip_101",
        }))

        service.order_persistence.get_order_by_id_only.assert_called_once_with(101)
        service.order_persistence.set_broker_order_id.assert_called_once_with(101, "20052000000017")
        service.trade_settlement_service.settle_fill.assert_called_once()

    def test_no_remarks_match_stays_ignored(self):
        service = self._service()
        service.order_persistence.get_order_by_broker_order_id.return_value = None

        _run(service.handle_order_update({
            "norenordno": "unknown", "reporttype": "Fill", "remarks": "not_our_tag",
        }))

        service.order_persistence.get_order_by_id_only.assert_not_called()
        service.trade_settlement_service.settle_fill.assert_not_called()

    def test_remarks_order_not_found_stays_ignored(self):
        service = self._service()
        service.order_persistence.get_order_by_broker_order_id.return_value = None
        service.order_persistence.get_order_by_id_only.return_value = None

        _run(service.handle_order_update({
            "norenordno": "unknown", "reporttype": "Fill", "remarks": "primepip_999",
        }))

        service.trade_settlement_service.settle_fill.assert_not_called()


class TestHandleFill:

    def _service(self):
        service = OrderUpdateService()
        service.order_persistence = MagicMock()
        service.order_service = MagicMock()
        service.trade_settlement_service = MagicMock()
        service.trade_history_service = MagicMock()
        service.trade_history_service.getFillStats.return_value = (101.15, 75)
        return service

    @patch("service.orderUpdateService.PostgresConnectionFactory")
    def test_full_fill_settles_and_marks_executed(self, mock_conn_factory):
        service = self._service()
        service.order_persistence.get_order_by_broker_order_id.return_value = _order_row()
        mock_conn_factory.create_connection.return_value = MagicMock()

        _run(service.handle_order_update({
            "norenordno": "20052000000017", "reporttype": "Fill",
            "flqty": "75", "flprc": "101.15",
        }))

        service.trade_settlement_service.settle_fill.assert_called_once()
        call_args = service.trade_settlement_service.settle_fill.call_args.args
        assert call_args[0] == 42  # user_id
        assert call_args[1] == "BUY"
        assert call_args[3] == 75  # fill qty
        assert call_args[4] == 101.15  # fill price
        service.order_service.update_order_status_single.assert_called_once()
        assert service.order_service.update_order_status_single.call_args.args[0] == "EXECUTED"

    @patch("service.orderUpdateService.PostgresConnectionFactory")
    def test_partial_fill_marks_partially_executed(self, mock_conn_factory):
        service = self._service()
        service.order_persistence.get_order_by_broker_order_id.return_value = _order_row(quantity=75)
        service.trade_history_service.getFillStats.return_value = (101.15, 30)
        mock_conn_factory.create_connection.return_value = MagicMock()

        _run(service.handle_order_update({
            "norenordno": "20052000000017", "reporttype": "Fill",
            "flqty": "30", "flprc": "101.15",
        }))

        assert service.order_service.update_order_status_single.call_args.args[0] == "PARTIALLY_EXECUTED"

    @patch("service.orderUpdateService.PostgresConnectionFactory")
    def test_sell_fill_populates_sell_side_trade_history_only(self, mock_conn_factory):
        service = self._service()
        service.order_persistence.get_order_by_broker_order_id.return_value = _order_row(side="SELL")
        mock_conn_factory.create_connection.return_value = MagicMock()

        _run(service.handle_order_update({
            "norenordno": "20052000000017", "reporttype": "Fill",
            "flqty": "75", "flprc": "101.15",
        }))

        insert_args = service.trade_history_service.insertTradeOrders.call_args.args
        buy_order_id, sell_order_id, buy_user_id, sell_user_id = insert_args[0], insert_args[1], insert_args[2], insert_args[3]
        assert buy_order_id is None and buy_user_id is None
        assert sell_order_id == 101 and sell_user_id == 42

    @patch("service.orderUpdateService.PostgresConnectionFactory")
    def test_duplicate_flid_is_ignored_not_double_settled(self, mock_conn_factory):
        """Shoonya's order-update feed can redeliver a Fill message (e.g. a
        WS resubscription after reconnect) - the second delivery of the same
        flid must not settle a second time."""
        from psycopg2.errors import UniqueViolation

        service = self._service()
        service.order_persistence.get_order_by_broker_order_id.return_value = _order_row()
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_conn_factory.create_connection.return_value = mock_conn

        def execute_side_effect(query, params=None):
            if "processed_broker_fills" in query:
                raise UniqueViolation("duplicate key value violates unique constraint")

        mock_cursor.execute.side_effect = execute_side_effect

        _run(service.handle_order_update({
            "norenordno": "20052000000017", "reporttype": "Fill",
            "flqty": "75", "flprc": "101.15", "flid": "FILL123",
        }))

        service.trade_settlement_service.settle_fill.assert_not_called()
        mock_conn.rollback.assert_called_once()

    @patch("service.orderUpdateService.PostgresConnectionFactory")
    def test_fill_with_new_flid_is_settled_normally(self, mock_conn_factory):
        service = self._service()
        service.order_persistence.get_order_by_broker_order_id.return_value = _order_row()
        mock_conn_factory.create_connection.return_value = MagicMock()

        _run(service.handle_order_update({
            "norenordno": "20052000000017", "reporttype": "Fill",
            "flqty": "75", "flprc": "101.15", "flid": "FILL123",
        }))

        service.trade_settlement_service.settle_fill.assert_called_once()

    def test_missing_flqty_flprc_is_ignored_not_crashed(self):
        service = self._service()
        service.order_persistence.get_order_by_broker_order_id.return_value = _order_row()
        _run(service.handle_order_update({"norenordno": "20052000000017", "reporttype": "Fill"}))
        service.trade_settlement_service.settle_fill.assert_not_called()

    @patch("service.orderUpdateService.PostgresConnectionFactory")
    def test_settlement_db_error_is_logged_not_raised(self, mock_conn_factory):
        service = self._service()
        service.order_persistence.get_order_by_broker_order_id.return_value = _order_row()
        service.trade_settlement_service.settle_fill.side_effect = Exception("connection reset")
        mock_conn_factory.create_connection.return_value = MagicMock()

        _run(service.handle_order_update({
            "norenordno": "20052000000017", "reporttype": "Fill",
            "flqty": "75", "flprc": "101.15",
        }))  # must not raise


class TestHandleRejected:

    def _service(self):
        service = OrderUpdateService()
        service.order_persistence = MagicMock()
        service.order_service = MagicMock()
        return service

    def test_pending_order_rejected_triggers_cancel(self):
        service = self._service()
        service.order_persistence.get_order_by_broker_order_id.return_value = _order_row(status="PENDING")

        _run(service.handle_order_update({
            "norenordno": "20052000000017", "reporttype": "Rejected", "rejreason": "RMS:Margin Exceeds",
        }))

        service.order_service.cancel_order_by_id.assert_called_once_with(42, 101)

    def test_already_reconciled_order_is_not_double_cancelled(self):
        service = self._service()
        service.order_persistence.get_order_by_broker_order_id.return_value = _order_row(status="CANCELLED")

        _run(service.handle_order_update({"norenordno": "20052000000017", "reporttype": "Rejected"}))

        service.order_service.cancel_order_by_id.assert_not_called()

    def test_cancel_failure_is_logged_not_raised(self):
        service = self._service()
        service.order_persistence.get_order_by_broker_order_id.return_value = _order_row(status="PENDING")
        service.order_service.cancel_order_by_id.side_effect = Exception("db exploded")

        _run(service.handle_order_update({"norenordno": "20052000000017", "reporttype": "Rejected"}))  # must not raise


class TestHandleCancelled:

    def _service(self):
        service = OrderUpdateService()
        service.order_persistence = MagicMock()
        service.order_service = MagicMock()
        return service

    def test_pending_order_cancelled_at_broker_triggers_cancel(self):
        service = self._service()
        service.order_persistence.get_order_by_broker_order_id.return_value = _order_row(status="PENDING")

        _run(service.handle_order_update({"norenordno": "20052000000017", "reporttype": "Canceled"}))

        service.order_service.cancel_order_by_id.assert_called_once_with(42, 101)

    def test_partially_executed_order_is_not_auto_reconciled(self):
        """Documented gap: cancelling the unfilled remainder of a partially-
        filled order isn't handled automatically (mirrors
        cancel_order_by_id's own PENDING/PENDING_TRIGGER-only scope) -
        must be logged, not silently ignored or incorrectly refunded."""
        service = self._service()
        service.order_persistence.get_order_by_broker_order_id.return_value = _order_row(status="PARTIALLY_EXECUTED")

        _run(service.handle_order_update({"norenordno": "20052000000017", "reporttype": "Canceled"}))

        service.order_service.cancel_order_by_id.assert_not_called()
