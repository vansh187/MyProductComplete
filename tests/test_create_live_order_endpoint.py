"""
Tests for POST /createLiveOrder (api/orders.py) - places a REAL order on the
Shoonya master account for F&O orders, separate from POST /orders (always
simulated/peer-matched).

No real DB/network/broker calls: OrderService, MarginEngine,
WalletBalanceService, and LiveOrderRoutingService are all mocked at the
api/orders.py call boundary.
"""

from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.orders import router
from utils.auth_dependency import get_current_user


TEST_ALGO_ID = "SEBI-ALGO-TEST-001"


@pytest.fixture(autouse=True)
def _algo_id_configured(monkeypatch):
    monkeypatch.setenv("SHOONYA_ALGO_ID", TEST_ALGO_ID)


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
        assert "RMS:Margin Exceeds" in resp.json()["detail"]["message"]
        assert resp.json()["detail"]["retry_with_new_client_order_id"] is True
        mock_order_service.cancel_rejected_order.assert_called_once_with(42, 101)

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
        assert resp.json()["detail"]["retry_with_new_client_order_id"] is False
        mock_order_service.cancel_order_by_id.assert_not_called()
        mock_order_service.cancel_rejected_order.assert_not_called()

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
        mock_order_service.cancel_rejected_order.assert_called_once_with(42, 101)


class TestCreateLiveOrderAlgoAndClientOrderId:

    @patch("api.orders.LiveOrderRoutingService")
    @patch("api.orders._create_order_row_with_checks")
    @patch("api.orders.MarginEngine")
    @patch("api.orders.live_orders_enabled", return_value=True)
    def test_algo_id_is_optional(self, mock_enabled, MockMargin, mock_create_row, MockRoutingService, monkeypatch):
        """No algo ID configured: the order still goes out, with algo_id=None."""
        monkeypatch.delenv("SHOONYA_ALGO_ID", raising=False)
        MockMargin.return_value.resolve_contract_type.return_value = {"contract_type": "OPTION", "lot_size": 75}
        mock_create_row.return_value = (101, "OPTION", {"contract_type": "OPTION", "lot_size": 75}, MagicMock(), MagicMock())
        MockRoutingService.return_value.place_live_order.return_value = {
            "broker_order_id": "20052000000017", "status": "PENDING", "raw_response": {},
        }

        resp = _make_client(_connected_shoonya()).post("/createLiveOrder", json=_live_order_payload())

        assert resp.status_code == 200
        _, kwargs = MockRoutingService.return_value.place_live_order.call_args
        assert kwargs["algo_id"] is None

    @patch("api.orders.LiveOrderRoutingService")
    @patch("api.orders._create_order_row_with_checks")
    @patch("api.orders.MarginEngine")
    @patch("api.orders.live_orders_enabled", return_value=True)
    def test_algo_id_is_attached_server_side_and_client_order_id_generated(self, mock_enabled, MockMargin, mock_create_row, MockRoutingService):
        MockMargin.return_value.resolve_contract_type.return_value = {"contract_type": "OPTION", "lot_size": 75}
        mock_create_row.return_value = (101, "OPTION", {"contract_type": "OPTION", "lot_size": 75}, MagicMock(), MagicMock())
        MockRoutingService.return_value.place_live_order.return_value = {
            "broker_order_id": "20052000000017", "status": "PENDING", "raw_response": {},
        }

        client = _make_client(_connected_shoonya())
        resp = client.post("/createLiveOrder", json=_live_order_payload())

        assert resp.status_code == 200
        generated_id = resp.json()["client_order_id"]
        assert generated_id.startswith("pp") and len(generated_id) == 22
        args, kwargs = MockRoutingService.return_value.place_live_order.call_args
        assert kwargs["algo_id"] == TEST_ALGO_ID
        assert args[0].client_order_id == generated_id
        # The BROKER tag comes from this server-side argument only.
        assert mock_create_row.call_args.kwargs["broker_routed"] is True

    @patch("api.orders.LiveOrderRoutingService")
    @patch("api.orders._create_order_row_with_checks")
    @patch("api.orders.OrderPersistence")
    @patch("api.orders.MarginEngine")
    @patch("api.orders.live_orders_enabled", return_value=True)
    def test_client_supplied_id_is_kept(self, mock_enabled, MockMargin, MockPersistence, mock_create_row, MockRoutingService):
        MockMargin.return_value.resolve_contract_type.return_value = {"contract_type": "OPTION", "lot_size": 75}
        MockPersistence.return_value.get_order_by_client_order_id.return_value = None
        mock_create_row.return_value = (101, "OPTION", {"contract_type": "OPTION", "lot_size": 75}, MagicMock(), MagicMock())
        MockRoutingService.return_value.place_live_order.return_value = {
            "broker_order_id": "20052000000017", "status": "PENDING", "raw_response": {},
        }

        client = _make_client(_connected_shoonya())
        resp = client.post("/createLiveOrder", json=_live_order_payload(client_order_id="algo-42"))

        assert resp.status_code == 200
        assert resp.json()["client_order_id"] == "algo-42"
        MockPersistence.return_value.get_order_by_client_order_id.assert_called_once_with(42, "algo-42")

    @patch("api.orders._create_order_row_with_checks")
    @patch("api.orders.OrderPersistence")
    @patch("api.orders.live_orders_enabled", return_value=True)
    def test_duplicate_client_order_id_returns_409_without_placing(self, mock_enabled, MockPersistence, mock_create_row):
        MockPersistence.return_value.get_order_by_client_order_id.return_value = {"id": 77}
        client = _make_client(_connected_shoonya())
        resp = client.post("/createLiveOrder", json=_live_order_payload(client_order_id="algo-42"))
        assert resp.status_code == 409
        assert resp.json()["detail"]["order_id"] == 77
        mock_create_row.assert_not_called()

    @pytest.mark.parametrize("bad_id", ["has space", "x" * 41, "primepip_101", "semi;colon"])
    @patch("api.orders._create_order_row_with_checks")
    @patch("api.orders.live_orders_enabled", return_value=True)
    def test_invalid_or_reserved_client_order_id_rejected(self, mock_enabled, mock_create_row, bad_id):
        client = _make_client(_connected_shoonya())
        resp = client.post("/createLiveOrder", json=_live_order_payload(client_order_id=bad_id))
        assert resp.status_code == 400
        mock_create_row.assert_not_called()


class TestGetOrderByClientOrderId:
    """The status service is swapped for a fresh instance per test (its
    order-book cache must not leak between tests) with a mocked persistence
    for the broker-linkage check/backfill."""

    @pytest.fixture(autouse=True)
    def _fresh_status_service(self):
        from service.brokerOrderStatusService import BrokerOrderStatusService
        self.status_persistence = MagicMock()
        self.status_persistence.get_order_by_broker_order_id.return_value = None
        with patch("api.orders._broker_order_status_service",
                   BrokerOrderStatusService(order_persistence=self.status_persistence)):
            yield

    def _order_row(self, **overrides):
        row = {
            "id": 101, "status": "PENDING", "broker_order_id": "20052000000017", "client_order_id": "algo-42",
            "source": "BROKER", "symbol": "NIFTY14JUL2623950CE", "side": "BUY", "quantity": 75,
        }
        row.update(overrides)
        return row

    def _book_entry(self, **overrides):
        entry = {"norenordno": "20052000000099", "remarks": "algo-42", "status": "OPEN", "fillshares": "0",
                 "tsym": "NIFTY14JUL2623950CE", "trantype": "B", "qty": "75"}
        entry.update(overrides)
        return entry

    @patch("api.orders.OrderPersistence")
    def test_returns_broker_status_fields(self, MockPersistence):
        MockPersistence.return_value.get_order_by_client_order_id.return_value = self._order_row()
        shoonya = _connected_shoonya()
        shoonya.get_order_history.return_value = [
            {"norenordno": "20052000000017", "status": "COMPLETE", "fillshares": "75", "avgprc": "101.15",
             "qty": "75", "norentm": "10:15:30 06-10-2026"},
            {"norenordno": "20052000000017", "status": "OPEN", "qty": "75"},
        ]

        resp = _make_client(shoonya).get("/orders/by-client-id/algo-42")

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "COMPLETE"
        assert body["filled_qty"] == 75
        assert body["avg_fill_price"] == 101.15
        assert body["rejreason"] is None
        assert body["broker_found"] is True
        assert body["routed_to_broker"] is True
        assert body["order_id"] == 101
        shoonya.get_order_history.assert_called_once_with("20052000000017")
        shoonya.get_order_book.assert_not_called()

    @patch("api.orders.OrderPersistence")
    def test_rejected_order_reports_rejreason(self, MockPersistence):
        MockPersistence.return_value.get_order_by_client_order_id.return_value = self._order_row()
        shoonya = _connected_shoonya()
        shoonya.get_order_history.return_value = [
            {"norenordno": "20052000000017", "status": "REJECTED", "rejreason": "RMS:Margin Exceeds", "avgprc": "0"},
        ]

        body = _make_client(shoonya).get("/orders/by-client-id/algo-42").json()

        assert body["status"] == "REJECTED"
        assert body["rejreason"] == "RMS:Margin Exceeds"
        assert body["filled_qty"] == 0
        assert body["avg_fill_price"] is None

    @patch("api.orders.OrderPersistence")
    def test_uncertain_order_is_found_in_order_book_and_linked(self, MockPersistence):
        MockPersistence.return_value.get_order_by_client_order_id.return_value = self._order_row(broker_order_id=None)
        shoonya = _connected_shoonya()
        shoonya.get_order_book.return_value = [
            self._book_entry(norenordno="1", remarks="someone-else"),
            self._book_entry(),
        ]

        body = _make_client(shoonya).get("/orders/by-client-id/algo-42").json()

        assert body["broker_found"] is True
        assert body["broker_order_id"] == "20052000000099"
        assert body["status"] == "OPEN"
        shoonya.get_order_history.assert_not_called()
        # Saved, so the next poll uses SingleOrdHist instead of the whole book.
        self.status_persistence.set_broker_order_id.assert_called_once_with(101, "20052000000099")

    @patch("api.orders.OrderPersistence")
    def test_same_remarks_but_another_users_contract_is_not_returned(self, MockPersistence):
        """Review finding: client_order_id is only unique per user - a remarks
        match on someone else's order (different contract/side/qty) must not
        be reported as this user's."""
        MockPersistence.return_value.get_order_by_client_order_id.return_value = self._order_row(broker_order_id=None)
        shoonya = _connected_shoonya()
        shoonya.get_order_book.return_value = [
            self._book_entry(tsym="NIFTY14JUL26P23950"),
            self._book_entry(trantype="S"),
            self._book_entry(qty="150"),
        ]

        body = _make_client(shoonya).get("/orders/by-client-id/algo-42").json()

        assert body["broker_found"] is False
        self.status_persistence.set_broker_order_id.assert_not_called()

    @patch("api.orders.OrderPersistence")
    def test_identical_order_already_linked_to_another_order_is_not_returned(self, MockPersistence):
        MockPersistence.return_value.get_order_by_client_order_id.return_value = self._order_row(broker_order_id=None)
        self.status_persistence.get_order_by_broker_order_id.return_value = {"id": 555}
        shoonya = _connected_shoonya()
        shoonya.get_order_book.return_value = [self._book_entry()]

        body = _make_client(shoonya).get("/orders/by-client-id/algo-42").json()

        assert body["broker_found"] is False
        self.status_persistence.set_broker_order_id.assert_not_called()

    @patch("api.orders.OrderPersistence")
    def test_two_unclaimed_matches_is_409_not_a_guess(self, MockPersistence):
        MockPersistence.return_value.get_order_by_client_order_id.return_value = self._order_row(broker_order_id=None)
        shoonya = _connected_shoonya()
        shoonya.get_order_book.return_value = [self._book_entry(), self._book_entry(norenordno="20052000000100")]

        assert _make_client(shoonya).get("/orders/by-client-id/algo-42").status_code == 409

    @patch("api.orders.OrderPersistence")
    def test_simulated_order_never_touches_the_broker(self, MockPersistence):
        MockPersistence.return_value.get_order_by_client_order_id.return_value = self._order_row(
            broker_order_id=None, source="SIMULATED"
        )
        shoonya = _connected_shoonya()

        body = _make_client(shoonya).get("/orders/by-client-id/algo-42").json()

        assert body["routed_to_broker"] is False
        assert body["broker_found"] is False
        shoonya.get_order_book.assert_not_called()
        shoonya.get_order_history.assert_not_called()

    @patch("api.orders.OrderPersistence")
    def test_order_book_is_shared_across_rapid_polls_when_found(self, MockPersistence):
        MockPersistence.return_value.get_order_by_client_order_id.return_value = self._order_row(broker_order_id=None)
        shoonya = _connected_shoonya()
        shoonya.get_order_book.return_value = [self._book_entry()]
        client = _make_client(shoonya)

        client.get("/orders/by-client-id/algo-42")
        client.get("/orders/by-client-id/algo-42")

        shoonya.get_order_book.assert_called_once()

    @patch("api.orders.OrderPersistence")
    def test_unseen_pending_order_is_unconfirmed_and_not_safe_to_retry(self, MockPersistence):
        """Review finding: after a timed-out placement the broker may not list
        the order yet - that must never read as "nothing was placed"."""
        MockPersistence.return_value.get_order_by_client_order_id.return_value = self._order_row(broker_order_id=None)
        shoonya = _connected_shoonya()
        shoonya.get_order_book.return_value = []

        resp = _make_client(shoonya).get("/orders/by-client-id/algo-42")

        assert resp.status_code == 200
        body = resp.json()
        assert body["broker_found"] is False
        assert body["status"] is None
        assert body["placement_status"] == "UNCONFIRMED"
        assert body["retry_with_new_client_order_id"] is False

    @patch("api.orders.OrderPersistence")
    def test_unknown_client_order_id_is_404(self, MockPersistence):
        MockPersistence.return_value.get_order_by_client_order_id.return_value = None
        resp = _make_client(_connected_shoonya()).get("/orders/by-client-id/nope")
        assert resp.status_code == 404

    @patch("api.orders.OrderPersistence")
    def test_broker_failure_is_503(self, MockPersistence):
        MockPersistence.return_value.get_order_by_client_order_id.return_value = self._order_row()
        shoonya = _connected_shoonya()
        shoonya.get_order_history.return_value = None
        resp = _make_client(shoonya).get("/orders/by-client-id/algo-42")
        assert resp.status_code == 503


class _UniqueViolation(Exception):
    pgcode = "23505"


class TestOrderRowCreationFailures:

    def _order(self, **overrides):
        from api.models import OrderCreate
        return OrderCreate(**_live_order_payload(client_order_id="algo-42", **overrides))

    @patch("api.orders.OrderService")
    @patch("api.orders.WalletBalanceService")
    @patch("api.orders.MarginEngine")
    def test_concurrent_duplicate_insert_is_409_and_refunds_the_debit(self, MockMargin, MockWallet, MockOrderService):
        """Review finding: two concurrent retries can both pass the pre-check;
        the unique index makes the second insert fail - that must be a 409 and
        must hand back the wallet debit taken just before the insert."""
        from fastapi import HTTPException
        from api.orders import _create_order_row_with_checks

        MockMargin.return_value.resolve_contract_type.return_value = {"contract_type": "OPTION", "lot_size": 75}
        MockWallet.return_value.debitWalletIfSufficient.return_value = True
        wrapped = Exception("Database error: duplicate key")
        wrapped.__cause__ = _UniqueViolation("duplicate key value violates unique constraint")
        MockOrderService.return_value.create_order.side_effect = wrapped

        with pytest.raises(HTTPException) as exc_info:
            _create_order_row_with_checks(self._order(), 42)

        assert exc_info.value.status_code == 409
        MockWallet.return_value.creditWalletStandalone.assert_called_once()

    @patch("api.orders.OrderService")
    @patch("api.orders.WalletBalanceService")
    @patch("api.orders.MarginEngine")
    def test_other_insert_errors_still_refund_and_propagate(self, MockMargin, MockWallet, MockOrderService):
        from api.orders import _create_order_row_with_checks

        MockMargin.return_value.resolve_contract_type.return_value = {"contract_type": "OPTION", "lot_size": 75}
        MockWallet.return_value.debitWalletIfSufficient.return_value = True
        MockOrderService.return_value.create_order.side_effect = Exception("connection lost")

        with pytest.raises(Exception, match="connection lost"):
            _create_order_row_with_checks(self._order(), 42)
        MockWallet.return_value.creditWalletStandalone.assert_called_once()

    @patch("api.orders.LiveOrderRoutingService")
    @patch("api.orders._create_order_row_with_checks")
    @patch("api.orders.MarginEngine")
    @patch("api.orders.live_orders_enabled", return_value=True)
    def test_failed_release_after_reject_is_surfaced_not_swallowed(self, mock_enabled, MockMargin, mock_create_row, MockRoutingService):
        """Review finding: if cancelling a broker-rejected order fails, its
        funds stay blocked - the client must not be told it was a clean reject."""
        from service.liveOrderRoutingService import LiveOrderRejectedError

        MockMargin.return_value.resolve_contract_type.return_value = {"contract_type": "OPTION", "lot_size": 75}
        mock_order_service = MagicMock()
        mock_order_service.cancel_rejected_order.side_effect = Exception("db blip")
        mock_create_row.return_value = (101, "OPTION", {"contract_type": "OPTION", "lot_size": 75}, MagicMock(), mock_order_service)
        MockRoutingService.return_value.place_live_order.side_effect = LiveOrderRejectedError("RMS:Margin Exceeds")

        resp = _make_client(_connected_shoonya()).post("/createLiveOrder", json=_live_order_payload())

        assert resp.status_code == 500
        assert "manual reconciliation" in resp.json()["detail"]["message"]



class TestPlacementStatus:

    @pytest.fixture(autouse=True)
    def _fresh_status_service(self):
        from service.brokerOrderStatusService import BrokerOrderStatusService
        self.status_persistence = MagicMock()
        self.status_persistence.get_order_by_broker_order_id.return_value = None
        with patch("api.orders._broker_order_status_service",
                   BrokerOrderStatusService(order_persistence=self.status_persistence)):
            yield

    def _row(self, **overrides):
        row = {
            "id": 101, "status": "PENDING", "broker_order_id": None, "client_order_id": "algo-42",
            "source": "BROKER", "symbol": "NIFTY14JUL2623950CE", "side": "BUY", "quantity": 75,
            "filled_qty": None, "avg_fill_price": None,
        }
        row.update(overrides)
        return row

    @patch("api.orders.OrderPersistence")
    def test_cancelled_order_absent_at_broker_is_closed_and_retryable(self, MockPersistence):
        MockPersistence.return_value.get_order_by_client_order_id.return_value = self._row(status="CANCELLED")
        shoonya = _connected_shoonya()
        shoonya.get_order_book.return_value = []

        body = _make_client(shoonya).get("/orders/by-client-id/algo-42").json()

        assert body["placement_status"] == "CLOSED"
        assert body["retry_with_new_client_order_id"] is True

    @patch("api.orders.OrderPersistence")
    def test_broker_rejected_with_no_fills_is_closed_and_retryable(self, MockPersistence):
        MockPersistence.return_value.get_order_by_client_order_id.return_value = self._row(broker_order_id="9")
        shoonya = _connected_shoonya()
        shoonya.get_order_history.return_value = [{"norenordno": "9", "status": "REJECTED", "rejreason": "RMS"}]

        body = _make_client(shoonya).get("/orders/by-client-id/algo-42").json()

        assert body["placement_status"] == "CLOSED"
        assert body["retry_with_new_client_order_id"] is True

    @patch("api.orders.OrderPersistence")
    def test_open_broker_order_is_confirmed_and_not_retryable(self, MockPersistence):
        MockPersistence.return_value.get_order_by_client_order_id.return_value = self._row(broker_order_id="9")
        shoonya = _connected_shoonya()
        shoonya.get_order_history.return_value = [{"norenordno": "9", "status": "OPEN"}]

        body = _make_client(shoonya).get("/orders/by-client-id/algo-42").json()

        assert body["placement_status"] == "CONFIRMED"
        assert body["retry_with_new_client_order_id"] is False

    @patch("api.orders.OrderPersistence")
    def test_not_routed_order_reports_its_own_fills(self, MockPersistence):
        """Review finding: fills must come from our record, not a hardcoded 0."""
        MockPersistence.return_value.get_order_by_client_order_id.return_value = self._row(
            source="SIMULATED", status="EXECUTED", filled_qty=75, avg_fill_price=Decimal("101.15"),
        )

        body = _make_client(_connected_shoonya()).get("/orders/by-client-id/algo-42").json()

        assert body["placement_status"] == "NOT_ROUTED"
        assert body["filled_qty"] == 75
        assert body["avg_fill_price"] == 101.15


class TestOrderBookFreshness:

    def test_cached_book_from_before_the_request_is_refetched_before_not_found(self):
        import asyncio
        import time
        from service.brokerOrderStatusService import BrokerOrderStatusService

        persistence = MagicMock()
        persistence.get_order_by_broker_order_id.return_value = None
        service = BrokerOrderStatusService(order_persistence=persistence)
        service._order_book, service._order_book_at = [], time.perf_counter()  # fresh by TTL, but pre-request
        shoonya = _connected_shoonya()
        shoonya.get_order_book.return_value = [{
            "norenordno": "77", "remarks": "algo-42", "status": "OPEN",
            "tsym": "NIFTY14JUL2623950CE", "trantype": "B", "qty": "75",
        }]
        row = {"id": 101, "status": "PENDING", "broker_order_id": None, "client_order_id": "algo-42",
               "source": "BROKER", "symbol": "NIFTY14JUL2623950CE", "side": "BUY", "quantity": 75}

        status, reason = asyncio.run(service.get_status(shoonya, row))

        assert reason is None and status["broker_order_id"] == "77"
        shoonya.get_order_book.assert_called_once()


class TestRejectCleanup:

    def _reject(self, MockMargin, mock_create_row, MockRoutingService, order_service):
        from service.liveOrderRoutingService import LiveOrderRejectedError
        MockMargin.return_value.resolve_contract_type.return_value = {"contract_type": "OPTION", "lot_size": 75}
        mock_create_row.return_value = (101, "OPTION", {"contract_type": "OPTION", "lot_size": 75}, MagicMock(), order_service)
        MockRoutingService.return_value.place_live_order.side_effect = LiveOrderRejectedError("RMS")
        return _make_client(_connected_shoonya()).post("/createLiveOrder", json=_live_order_payload())

    @patch("api.orders.LiveOrderRoutingService")
    @patch("api.orders._create_order_row_with_checks")
    @patch("api.orders.MarginEngine")
    @patch("api.orders.live_orders_enabled", return_value=True)
    def test_already_closed_by_broker_push_is_a_clean_reject(self, mock_enabled, MockMargin, mock_create_row, MockRoutingService):
        order_service = MagicMock()
        order_service.cancel_rejected_order.return_value = False
        order_service.get_order_by_id.return_value = {"id": 101, "status": "CANCELLED"}
        assert self._reject(MockMargin, mock_create_row, MockRoutingService, order_service).status_code == 400

    @patch("api.orders.LiveOrderRoutingService")
    @patch("api.orders._create_order_row_with_checks")
    @patch("api.orders.MarginEngine")
    @patch("api.orders.live_orders_enabled", return_value=True)
    def test_still_open_after_reject_is_500(self, mock_enabled, MockMargin, mock_create_row, MockRoutingService):
        """Review finding: a False cancel result was ignored."""
        order_service = MagicMock()
        order_service.cancel_rejected_order.return_value = False
        order_service.get_order_by_id.return_value = {"id": 101, "status": "PENDING"}
        assert self._reject(MockMargin, mock_create_row, MockRoutingService, order_service).status_code == 500

    @patch("api.orders.LiveOrderRoutingService")
    @patch("api.orders._create_order_row_with_checks")
    @patch("api.orders.MarginEngine")
    @patch("api.orders.live_orders_enabled", return_value=True)
    def test_margin_release_failure_after_reject_is_500(self, mock_enabled, MockMargin, mock_create_row, MockRoutingService):
        """Review finding: MarginEngineError was only logged inside the cancel."""
        from service.orderService import FundsReleaseError
        order_service = MagicMock()
        order_service.cancel_rejected_order.side_effect = FundsReleaseError(101, ["margin release: boom"])
        resp = self._reject(MockMargin, mock_create_row, MockRoutingService, order_service)
        assert resp.status_code == 500
        assert "manual reconciliation" in resp.json()["detail"]["message"]


class TestLiveOrderMarketRules:

    def _post(self, MockMargin, mock_create_row, instrument=None, **payload):
        MockMargin.return_value.resolve_contract_type.return_value = instrument or {"contract_type": "OPTION", "lot_size": 75}
        return _make_client(_connected_shoonya()).post("/createLiveOrder", json=_live_order_payload(**payload))

    @patch("api.orders._create_order_row_with_checks")
    @patch("api.orders.MarginEngine")
    @patch("api.orders.live_orders_enabled", return_value=True)
    def test_expired_contract_is_rejected_before_any_db_write(self, mock_enabled, MockMargin, mock_create_row):
        resp = self._post(MockMargin, mock_create_row,
                          instrument={"contract_type": "OPTION", "lot_size": 75, "expiry": "2026-01-06"})
        assert resp.status_code == 400
        assert "expired" in resp.json()["detail"]
        mock_create_row.assert_not_called()

    @patch("api.orders.OptionMaster.find_by_tsym", return_value={"tick_size": 0.05})
    @patch("api.orders._create_order_row_with_checks")
    @patch("api.orders.MarginEngine")
    @patch("api.orders.live_orders_enabled", return_value=True)
    def test_price_off_tick_is_rejected(self, mock_enabled, MockMargin, mock_create_row, mock_find):
        resp = self._post(MockMargin, mock_create_row, price=101.13)
        assert resp.status_code == 400
        assert "tick size" in resp.json()["detail"]
        mock_create_row.assert_not_called()

    @patch("api.orders.OptionMaster.find_by_tsym", return_value={"tick_size": 0.05})
    @patch("api.orders._create_order_row_with_checks")
    @patch("api.orders.MarginEngine")
    @patch("api.orders.live_orders_enabled", return_value=True)
    def test_buy_stop_limit_trigger_above_limit_is_rejected(self, mock_enabled, MockMargin, mock_create_row, mock_find):
        resp = self._post(MockMargin, mock_create_row, order_type="STOPLIMIT", price=100.0, trigger_price=100.5)
        assert resp.status_code == 400
        assert "trigger price" in resp.json()["detail"]
        mock_create_row.assert_not_called()

    @patch("api.orders.LiveOrderRoutingService")
    @patch("api.orders.OptionMaster.find_by_tsym", return_value={"tick_size": 0.05})
    @patch("api.orders._create_order_row_with_checks")
    @patch("api.orders.MarginEngine")
    @patch("api.orders.live_orders_enabled", return_value=True)
    def test_valid_sell_stop_limit_passes(self, mock_enabled, MockMargin, mock_create_row, mock_find, MockRoutingService):
        mock_create_row.return_value = (101, "OPTION", {"contract_type": "OPTION", "lot_size": 75}, MagicMock(), MagicMock())
        MockRoutingService.return_value.place_live_order.return_value = {"broker_order_id": "1", "status": "PENDING", "raw_response": {}}
        resp = self._post(MockMargin, mock_create_row, side="SELL", order_type="STOPLIMIT", price=100.0, trigger_price=100.5)
        assert resp.status_code == 200


class TestDuplicateClientOrderIdDetail:

    @pytest.mark.parametrize("status, retry", [("CANCELLED", True), ("PENDING", False), ("EXECUTED", False)])
    @patch("api.orders._create_order_row_with_checks")
    @patch("api.orders.OrderPersistence")
    @patch("api.orders.live_orders_enabled", return_value=True)
    def test_409_says_whether_a_new_id_is_safe(self, mock_enabled, MockPersistence, mock_create_row, status, retry):
        MockPersistence.return_value.get_order_by_client_order_id.return_value = {"id": 77, "status": status}
        resp = _make_client(_connected_shoonya()).post("/createLiveOrder", json=_live_order_payload(client_order_id="algo-42"))
        assert resp.status_code == 409
        assert resp.json()["detail"]["status"] == status
        assert resp.json()["detail"]["retry_with_new_client_order_id"] is retry


class TestInternalEndpointsNeverTouchBrokerOrders:

    @patch("api.orders.OrderService")
    def test_unlinked_broker_order_cannot_be_cancelled_internally(self, MockOrderService):
        """A timed-out placement has no broker_order_id yet but may be live -
        an internal cancel would refund money for a real resting order."""
        MockOrderService.return_value.get_order_by_id.return_value = {
            "id": 101, "status": "PENDING", "broker_order_id": None, "source": "BROKER",
        }
        resp = _make_client(_connected_shoonya()).post("/orders/101/cancel")
        assert resp.status_code == 400
        MockOrderService.return_value.cancel_order_by_id.assert_not_called()

    @patch("api.orders.OrderService")
    def test_unlinked_broker_order_cannot_be_modified_internally(self, MockOrderService):
        MockOrderService.return_value.get_order_by_id.return_value = {
            "id": 101, "status": "PENDING", "broker_order_id": None, "source": "BROKER",
        }
        resp = _make_client(_connected_shoonya()).put("/orders/101", json={"price": 99.0})
        assert resp.status_code == 400
        MockOrderService.return_value.modify_order_by_id.assert_not_called()
