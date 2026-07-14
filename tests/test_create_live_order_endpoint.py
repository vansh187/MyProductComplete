"""
Tests for POST /createLiveOrder (api/orders.py) - places a REAL order on the
Shoonya master account for F&O orders, separate from POST /orders (always
simulated/peer-matched).

No real DB/network/broker calls: OrderService, MarginEngine,
WalletBalanceService, and LiveOrderRoutingService are all mocked at the
api/orders.py call boundary.
"""

from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.orders import router
from utils.auth_dependency import get_current_user


def _make_client(shoonya_state=None):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: {"user_id": 42}
    if shoonya_state is not None:
        app.state.shoonya = shoonya_state
    return TestClient(app)


def _connected_shoonya():
    shoonya = MagicMock()
    shoonya.is_connected = True
    shoonya._api = MagicMock()
    return shoonya


def _live_order_payload(**overrides):
    payload = {
        "symbol": "NIFTY14JUL2623950CE", "exchange": "NFO", "side": "BUY",
        "quantity": 75, "order_type": "LIMIT", "price": 101.15,
        "product_type": "MIS", "validity": "DAY",
    }
    payload.update(overrides)
    return payload


class TestCreateLiveOrderGating:

    @patch("api.orders.live_orders_enabled", return_value=False)
    def test_disabled_flag_rejects_before_touching_anything(self, mock_enabled):
        client = _make_client(_connected_shoonya())
        resp = client.post("/createLiveOrder", json=_live_order_payload())
        assert resp.status_code == 400
        assert "disabled" in resp.json()["detail"].lower()

    @patch("api.orders.live_orders_enabled", return_value=True)
    def test_no_shoonya_session_returns_503(self, mock_enabled):
        client = _make_client(shoonya_state=None)
        resp = client.post("/createLiveOrder", json=_live_order_payload())
        assert resp.status_code == 503

    @patch("api.orders.live_orders_enabled", return_value=True)
    def test_disconnected_session_returns_503(self, mock_enabled):
        shoonya = MagicMock()
        shoonya.is_connected = False
        shoonya._api = MagicMock()
        client = _make_client(shoonya)
        resp = client.post("/createLiveOrder", json=_live_order_payload())
        assert resp.status_code == 503

    @patch("api.orders.MarginEngine")
    @patch("api.orders.live_orders_enabled", return_value=True)
    def test_equity_symbol_is_rejected(self, mock_enabled, MockMargin):
        MockMargin.return_value.resolve_contract_type.return_value = {"contract_type": None, "lot_size": None}
        client = _make_client(_connected_shoonya())
        resp = client.post("/createLiveOrder", json=_live_order_payload(symbol="RELIANCE", exchange="NSE"))
        assert resp.status_code == 400
        assert "F&O" in resp.json()["detail"]

    @patch("api.orders.MarginEngine")
    @patch("api.orders.live_orders_enabled", return_value=True)
    def test_bad_lot_size_rejected_before_order_creation(self, mock_enabled, MockMargin):
        MockMargin.return_value.resolve_contract_type.return_value = {"contract_type": "OPTION", "lot_size": 75}
        client = _make_client(_connected_shoonya())
        resp = client.post("/createLiveOrder", json=_live_order_payload(quantity=80))
        assert resp.status_code == 400
        assert "lot size" in resp.json()["detail"].lower()


class TestCreateLiveOrderPlacement:

    @patch("api.orders.LiveOrderRoutingService")
    @patch("api.orders._create_order_row_with_checks")
    @patch("api.orders.MarginEngine")
    @patch("api.orders.live_orders_enabled", return_value=True)
    def test_successful_placement_returns_broker_order_id(self, mock_enabled, MockMargin, mock_create_row, MockRoutingService):
        MockMargin.return_value.resolve_contract_type.return_value = {"contract_type": "OPTION", "lot_size": 75}
        mock_create_row.return_value = (101, "OPTION", {"contract_type": "OPTION", "lot_size": 75}, MagicMock(), MagicMock())
        MockRoutingService.return_value.place_live_order.return_value = {
            "broker_order_id": "20052000000017", "status": "PENDING", "raw_response": {},
        }

        client = _make_client(_connected_shoonya())
        resp = client.post("/createLiveOrder", json=_live_order_payload())

        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        assert body["order_id"] == 101
        assert body["broker_order_id"] == "20052000000017"

    @patch("api.orders.LiveOrderRoutingService")
    @patch("api.orders._create_order_row_with_checks")
    @patch("api.orders.MarginEngine")
    @patch("api.orders.live_orders_enabled", return_value=True)
    def test_broker_rejection_cancels_the_internal_order(self, mock_enabled, MockMargin, mock_create_row, MockRoutingService):
        from service.liveOrderRoutingService import LiveOrderRejectedError

        MockMargin.return_value.resolve_contract_type.return_value = {"contract_type": "OPTION", "lot_size": 75}
        mock_order_service = MagicMock()
        mock_create_row.return_value = (101, "OPTION", {"contract_type": "OPTION", "lot_size": 75}, MagicMock(), mock_order_service)
        MockRoutingService.return_value.place_live_order.side_effect = LiveOrderRejectedError("RMS:Margin Exceeds")

        client = _make_client(_connected_shoonya())
        resp = client.post("/createLiveOrder", json=_live_order_payload())

        assert resp.status_code == 400
        assert "RMS:Margin Exceeds" in resp.json()["detail"]
        mock_order_service.cancel_order_by_id.assert_called_once_with(42, 101)

    @patch("api.orders.LiveOrderRoutingService")
    @patch("api.orders._create_order_row_with_checks")
    @patch("api.orders.MarginEngine")
    @patch("api.orders.live_orders_enabled", return_value=True)
    def test_status_uncertain_does_not_cancel_the_order(self, mock_enabled, MockMargin, mock_create_row, MockRoutingService):
        """A timeout/ambiguous broker response must NOT trigger an automatic
        cancel - the order may have actually gone through."""
        from service.liveOrderRoutingService import LiveOrderStatusUncertainError

        MockMargin.return_value.resolve_contract_type.return_value = {"contract_type": "OPTION", "lot_size": 75}
        mock_order_service = MagicMock()
        mock_create_row.return_value = (101, "OPTION", {"contract_type": "OPTION", "lot_size": 75}, MagicMock(), mock_order_service)
        MockRoutingService.return_value.place_live_order.side_effect = LiveOrderStatusUncertainError("timeout")

        client = _make_client(_connected_shoonya())
        resp = client.post("/createLiveOrder", json=_live_order_payload())

        assert resp.status_code == 202
        mock_order_service.cancel_order_by_id.assert_not_called()

    @patch("api.orders.LiveOrderRoutingService")
    @patch("api.orders._create_order_row_with_checks")
    @patch("api.orders.MarginEngine")
    @patch("api.orders.live_orders_enabled", return_value=True)
    def test_lot_size_mismatch_at_placement_time_cancels_the_order(self, mock_enabled, MockMargin, mock_create_row, MockRoutingService):
        """The pre-check uses a separate probe resolution; the actual
        placement re-validates against the authoritative instrument from
        _create_order_row_with_checks - if that one disagrees, the order
        must still be cleanly cancelled, not left dangling."""
        from service.liveOrderRoutingService import LotSizeMismatchError

        MockMargin.return_value.resolve_contract_type.return_value = {"contract_type": "OPTION", "lot_size": 75}
        mock_order_service = MagicMock()
        mock_create_row.return_value = (101, "OPTION", {"contract_type": "OPTION", "lot_size": 75}, MagicMock(), mock_order_service)
        MockRoutingService.return_value.place_live_order.side_effect = LotSizeMismatchError(80, 75)

        client = _make_client(_connected_shoonya())
        resp = client.post("/createLiveOrder", json=_live_order_payload())

        assert resp.status_code == 400
        mock_order_service.cancel_order_by_id.assert_called_once_with(42, 101)
