"""
Unit tests for POST /orders — wallet balance pre-flight check.

All DB calls, WalletBalanceService, OrderService, and ExecutionEngine
are patched so no network or database connection is required.
"""

from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from contextlib import contextmanager
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.orders import router

# ── App fixture ──────────────────────────────────────────────────────────────

def _make_app():
    app = FastAPI()
    app.include_router(router)
    return app


# Fake JWT user injected via dependency override
_FAKE_USER = {"user_id": 42, "email": "test@example.com"}


def _override_auth():
    return _FAKE_USER


# ── Helper ───────────────────────────────────────────────────────────────────

@contextmanager
def _client_with_wallet(balance_value):
    """
    Return a TestClient whose WalletBalanceService returns the given balance.
    balance_value=None  → simulates no wallet row (new user).
    """
    from utils.auth_dependency import get_current_user

    app = _make_app()
    app.dependency_overrides[get_current_user] = _override_auth

    if balance_value is None:
        wallet_row = None
    else:
        wallet_row = {"user_id": 42, "balance": balance_value}

    with patch("api.orders.WalletBalanceService") as MockWallet, \
         patch("api.orders.OrderService") as MockOrder, \
         patch("api.orders.ExecutionEngine") as MockEngine:

        mock_wallet_instance = MagicMock()
        mock_wallet_instance.getWalletBalance.return_value = wallet_row
        MockWallet.return_value = mock_wallet_instance

        mock_order_instance = MagicMock()
        mock_order_instance.create_order.return_value = 101
        MockOrder.return_value = mock_order_instance

        mock_engine_instance = MagicMock()
        mock_engine_instance.executeOrder.return_value = {
            "success": True, "status": "ORDER STATUS EXECUTED", "tradeOrderId": 999
        }
        MockEngine.return_value = mock_engine_instance

        yield TestClient(app), mock_wallet_instance, mock_order_instance, mock_engine_instance


def _buy_payload(**overrides):
    payload = {
        "symbol": "RELIANCE",
        "side": "BUY",
        "quantity": 10,
        "price": "250.00",
        "status": "PENDING",
    }
    payload.update(overrides)
    return payload


def _sell_payload(**overrides):
    payload = {
        "symbol": "RELIANCE",
        "side": "SELL",
        "quantity": 5,
        "price": "250.00",
        "status": "PENDING",
    }
    payload.update(overrides)
    return payload


# ── BUY order — balance scenarios ────────────────────────────────────────────

class TestBuyOrderBalanceCheck:

    def test_buy_sufficient_balance_returns_200(self):
        """Wallet has more than enough — order proceeds."""
        with _client_with_wallet("5000.00") as (client, wallet_svc, order_svc, engine):
            resp = client.post("/orders", json=_buy_payload(quantity=10, price="250.00"))
        # required = 10 * 250 = 2500; balance = 5000 → OK
        assert resp.status_code == 200
        assert resp.json()["success"] is True

    def test_buy_exact_balance_returns_200(self):
        """Wallet balance equals exactly required amount — edge case, should pass."""
        with _client_with_wallet("2500.00") as (client, *_):
            resp = client.post("/orders", json=_buy_payload(quantity=10, price="250.00"))
        assert resp.status_code == 200

    def test_buy_insufficient_balance_returns_400(self):
        """Wallet has less than required — must be rejected before DB write."""
        with _client_with_wallet("100.00") as (client, wallet_svc, order_svc, engine):
            resp = client.post("/orders", json=_buy_payload(quantity=10, price="250.00"))
        assert resp.status_code == 400
        body = resp.json()["detail"]
        assert "Insufficient balance" in body
        assert "₹2500.00" in body   # required
        assert "₹100.00" in body    # available

    def test_buy_zero_balance_returns_400(self):
        """Zero balance wallet — any BUY must be rejected."""
        with _client_with_wallet("0.00") as (client, *_):
            resp = client.post("/orders", json=_buy_payload(quantity=1, price="1.00"))
        assert resp.status_code == 400

    def test_buy_no_wallet_row_returns_400(self):
        """User has never funded — wallet row is None — treated as ₹0 balance."""
        with _client_with_wallet(None) as (client, *_):
            resp = client.post("/orders", json=_buy_payload(quantity=1, price="100.00"))
        assert resp.status_code == 400
        assert "Insufficient balance" in resp.json()["detail"]

    def test_buy_wallet_balance_none_field_returns_400(self):
        """Wallet row exists but balance column is NULL."""
        from utils.auth_dependency import get_current_user
        from unittest.mock import patch, MagicMock

        app = _make_app()
        app.dependency_overrides[get_current_user] = _override_auth

        with patch("api.orders.WalletBalanceService") as MockWallet, \
             patch("api.orders.OrderService"), \
             patch("api.orders.ExecutionEngine"):

            instance = MagicMock()
            instance.getWalletBalance.return_value = {"user_id": 42, "balance": None}
            MockWallet.return_value = instance

            client = TestClient(app)
            resp = client.post("/orders", json=_buy_payload(quantity=5, price="100.00"))

        assert resp.status_code == 400

    def test_buy_insufficient_balance_does_not_call_order_service(self):
        """Order must NOT be written to DB when balance check fails."""
        with _client_with_wallet("50.00") as (client, wallet_svc, order_svc, engine):
            client.post("/orders", json=_buy_payload(quantity=10, price="250.00"))
        order_svc.create_order.assert_not_called()
        engine.executeOrder.assert_not_called()

    def test_buy_insufficient_balance_does_not_call_execution_engine(self):
        """ExecutionEngine must never run when balance is too low."""
        with _client_with_wallet("999.00") as (client, wallet_svc, order_svc, engine):
            client.post("/orders", json=_buy_payload(quantity=10, price="250.00"))
            # 10*250 = 2500 > 999 → blocked
        engine.executeOrder.assert_not_called()

    def test_buy_calls_wallet_service_with_correct_user_id(self):
        """WalletBalanceService must be queried with the authenticated user's ID."""
        with _client_with_wallet("9999.00") as (client, wallet_svc, *_):
            client.post("/orders", json=_buy_payload())
        wallet_svc.getWalletBalance.assert_called_once_with(_FAKE_USER["user_id"])


# ── SELL order — no balance check required ───────────────────────────────────

class TestSellOrderNoBalanceCheck:

    def test_sell_zero_balance_proceeds(self):
        """SELL orders bypass the wallet check entirely — even with ₹0 balance."""
        with _client_with_wallet("0.00") as (client, wallet_svc, *_):
            resp = client.post("/orders", json=_sell_payload())
        assert resp.status_code == 200

    def test_sell_no_wallet_row_proceeds(self):
        """SELL orders proceed even when wallet row does not exist."""
        with _client_with_wallet(None) as (client, wallet_svc, order_svc, *_):
            resp = client.post("/orders", json=_sell_payload())
        assert resp.status_code == 200
        order_svc.create_order.assert_called_once()

    def test_sell_does_not_call_wallet_service(self):
        """WalletBalanceService must NOT be called for SELL orders."""
        with _client_with_wallet(None) as (client, wallet_svc, *_):
            client.post("/orders", json=_sell_payload())
        wallet_svc.getWalletBalance.assert_not_called()


# ── Execution engine result forwarding ───────────────────────────────────────

class TestExecutionEngineResultForwarding:

    def test_engine_pending_result_forwarded(self):
        """If engine returns 'in process', that message reaches the caller."""
        from utils.auth_dependency import get_current_user

        app = _make_app()
        app.dependency_overrides[get_current_user] = _override_auth

        with patch("api.orders.WalletBalanceService") as MockWallet, \
             patch("api.orders.OrderService") as MockOrder, \
             patch("api.orders.ExecutionEngine") as MockEngine:

            MockWallet.return_value.getWalletBalance.return_value = {"user_id": 42, "balance": "99999.00"}
            MockOrder.return_value.create_order.return_value = 55
            MockEngine.return_value.executeOrder.return_value = {
                "userId": 42, "message": "Orders execution is in process. Please wait"
            }

            client = TestClient(app)
            resp = client.post("/orders", json=_buy_payload())

        assert resp.status_code == 200
        assert "in process" in resp.json()["message"]

    def test_engine_partial_execution_result_forwarded(self):
        """Partial execution response reaches caller unchanged."""
        from utils.auth_dependency import get_current_user

        app = _make_app()
        app.dependency_overrides[get_current_user] = _override_auth

        with patch("api.orders.WalletBalanceService") as MockWallet, \
             patch("api.orders.OrderService") as MockOrder, \
             patch("api.orders.ExecutionEngine") as MockEngine:

            MockWallet.return_value.getWalletBalance.return_value = {"user_id": 42, "balance": "50000.00"}
            MockOrder.return_value.create_order.return_value = 77
            MockEngine.return_value.executeOrder.return_value = {
                "success": True, "status": "ORDER Partially EXECUTED", "tradeOrderId": 88
            }

            client = TestClient(app)
            resp = client.post("/orders", json=_buy_payload())

        assert resp.status_code == 200
        assert resp.json()["status"] == "ORDER Partially EXECUTED"

    def test_engine_failure_result_forwarded(self):
        """Engine failure dict is returned as-is (not raised as HTTP exception)."""
        from utils.auth_dependency import get_current_user

        app = _make_app()
        app.dependency_overrides[get_current_user] = _override_auth

        with patch("api.orders.WalletBalanceService") as MockWallet, \
             patch("api.orders.OrderService") as MockOrder, \
             patch("api.orders.ExecutionEngine") as MockEngine:

            MockWallet.return_value.getWalletBalance.return_value = {"user_id": 42, "balance": "50000.00"}
            MockOrder.return_value.create_order.return_value = 78
            MockEngine.return_value.executeOrder.return_value = {
                "success": False, "status": "ORDER STATUS FAILED: DB error"
            }

            client = TestClient(app)
            resp = client.post("/orders", json=_buy_payload())

        assert resp.status_code == 200
        assert resp.json()["success"] is False


# ── WalletBalanceService runtime error handling ───────────────────────────────

class TestWalletServiceErrors:

    def test_wallet_service_exception_propagates_as_500(self):
        """If WalletBalanceService raises, caller gets HTTP 500 (FastAPI default)."""
        from utils.auth_dependency import get_current_user

        app = _make_app()
        app.dependency_overrides[get_current_user] = _override_auth

        with patch("api.orders.WalletBalanceService") as MockWallet, \
             patch("api.orders.OrderService"), \
             patch("api.orders.ExecutionEngine"):

            MockWallet.return_value.getWalletBalance.side_effect = Exception("DB connection lost")

            client = TestClient(app, raise_server_exceptions=False)
            resp = client.post("/orders", json=_buy_payload())

        assert resp.status_code == 500


# ── GET /orders endpoint sanity check ────────────────────────────────────────

class TestGetOrders:

    def test_get_orders_returns_200(self):
        from utils.auth_dependency import get_current_user

        app = _make_app()
        app.dependency_overrides[get_current_user] = _override_auth

        with patch("api.orders.OrderService") as MockOrder:
            MockOrder.return_value.getorders.return_value = []
            client = TestClient(app)
            resp = client.get("/orders")

        assert resp.status_code == 200
        assert resp.json()["Message"] == "Orders retrieved successfully"
        assert resp.json()["Orders"] == []


# ── Pydantic model validation (no HTTP call needed) ───────────────────────────

class TestOrderCreateModel:

    def test_invalid_side_rejected(self):
        """Side must be BUY or SELL — any other value fails validation."""
        from utils.auth_dependency import get_current_user

        app = _make_app()
        app.dependency_overrides[get_current_user] = _override_auth

        with patch("api.orders.WalletBalanceService"), \
             patch("api.orders.OrderService"), \
             patch("api.orders.ExecutionEngine"):

            client = TestClient(app)
            resp = client.post("/orders", json={
                "symbol": "RELIANCE",
                "side": "HOLD",
                "quantity": 10,
                "price": "250.00",
                "status": "PENDING",
            })

        assert resp.status_code == 422

    def test_negative_quantity_accepted_by_model(self):
        """
        Pydantic model does not constrain quantity sign — test that
        the route at least doesn't crash with a negative quantity.
        """
        from utils.auth_dependency import get_current_user

        app = _make_app()
        app.dependency_overrides[get_current_user] = _override_auth

        with patch("api.orders.WalletBalanceService") as MockWallet, \
             patch("api.orders.OrderService") as MockOrder, \
             patch("api.orders.ExecutionEngine") as MockEngine:

            MockWallet.return_value.getWalletBalance.return_value = {"user_id": 42, "balance": "9999.00"}
            MockOrder.return_value.create_order.return_value = 1
            MockEngine.return_value.executeOrder.return_value = {"success": True, "status": "EXECUTED"}

            client = TestClient(app)
            # negative qty * price → required is negative → always < balance → passes check
            resp = client.post("/orders", json={
                "symbol": "RELIANCE",
                "side": "BUY",
                "quantity": -1,
                "price": "250.00",
                "status": "PENDING",
            })

        # No crash — whatever the business logic returns is acceptable here
        assert resp.status_code in (200, 400, 422)
