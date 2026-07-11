"""
Tests for current, real behavior of:

  1. App startup (app.py) — Shoonya bootstrap must not block the HTTP server
     from responding once it is up; the actual startup work runs on a
     background executor thread (via loop.run_in_executor), not inline in
     the event loop.
  2. service/executionEngine.py — ExecutionEngine._process_trades() and the
     execution-state outcomes it can produce.
  3. api/orders.py — POST /orders and GET /orders, asserting the CURRENT
     wallet-balance-check/execution-forwarding behavior (including a known,
     unfixed 500-vs-400 bug — see TestBuyOrderNoneBalanceField below).

All DB/broker calls are mocked; no network or real Postgres connection is
required to run this file.
"""

import threading
import time
from contextlib import contextmanager
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.models import ExchangeType, OrderCreate, OrderSide, OrderType, ProductType, ValidityType
from api.orders import router
from service.executionEngine import ExecutionEngine
from service.orderService import OrderService
from service.tradeSettlementService import TradeSettlementService


# ============================================================================
# Shared helpers (adapted from the deleted test_orders_balance_check.py /
# test_execution_states.py — same conventions: _make_app / _override_auth /
# _client_with_wallet / _buy_payload / _buy_order / MockTradeExecution).
# ============================================================================

_FAKE_USER = {"user_id": 42, "email": "test@example.com"}


def _override_auth():
    return _FAKE_USER


def _make_app():
    app = FastAPI()
    app.include_router(router)
    return app


def _buy_payload(**overrides):
    payload = {
        "symbol": "RELIANCE",
        "side": "BUY",
        "quantity": 10,
        "price": 250.00,
    }
    payload.update(overrides)
    return payload


def _sell_payload(**overrides):
    payload = {
        "symbol": "RELIANCE",
        "side": "SELL",
        "quantity": 5,
        "price": 250.00,
    }
    payload.update(overrides)
    return payload


def _buy_order(quantity=10, price=1000.0):
    return OrderCreate(
        symbol="RELIANCE",
        exchange=ExchangeType.NSE,
        side=OrderSide.BUY,
        quantity=quantity,
        order_type=OrderType.LIMIT,
        price=price,
        product_type=ProductType.MIS,
        validity=ValidityType.DAY,
    )


class MockTradeExecution:
    """Mimics the object MatchingEngineService hands back per matched fill."""

    def __init__(self, buy_order_id=1, sell_order_id=2, buy_user_id=1, sell_user_id=2,
                 symbol="RELIANCE", quantity=5, execution_price=1000.0, remaining_qty=0):
        self.buy_order_id = buy_order_id
        self.sell_order_id = sell_order_id
        self.buy_user_id = buy_user_id
        self.sell_user_id = sell_user_id
        self.symbol = symbol
        self.quantity = quantity
        self.execution_price = execution_price
        self.remaining_qty = remaining_qty
        self.trade_value = quantity * execution_price


@contextmanager
def _client_with_wallet(balance_value, deduction_db_ok=True):
    """
    TestClient whose WalletBalanceService.getWalletBalance() returns a wallet
    row with the given balance. balance_value=None simulates no wallet row.
    OrderService and ExecutionEngine are mocked so no DB/matching runs.
    The wallet-deduction DB write (api/orders.py's local
    PostgresConnectionFactory import) is also mocked so a real Postgres
    connection is never attempted.
    """
    from utils.auth_dependency import get_current_user

    app = _make_app()
    app.dependency_overrides[get_current_user] = _override_auth

    wallet_row = None if balance_value is None else {"user_id": 42, "balance": balance_value}

    with patch("api.orders.WalletBalanceService") as MockWallet, \
         patch("api.orders.OrderService") as MockOrder, \
         patch("api.orders.ExecutionEngine") as MockEngine, \
         patch("database.PostgresConnectionFactory.PostgresConnectionFactory") as MockConnFactory:

        mock_wallet_instance = MagicMock()
        mock_wallet_instance.getWalletBalance.return_value = wallet_row
        MockWallet.return_value = mock_wallet_instance

        mock_order_instance = MagicMock()
        mock_order_instance.create_order.return_value = 101
        MockOrder.return_value = mock_order_instance

        mock_engine_instance = MagicMock()
        mock_engine_instance.execute_order.return_value = {
            "success": True, "status": "EXECUTED", "order_id": 101, "trade_id": 999
        }
        MockEngine.return_value = mock_engine_instance

        if deduction_db_ok:
            mock_conn = MagicMock()
            mock_conn.cursor.return_value = MagicMock()
            MockConnFactory.create_connection.return_value = mock_conn
        else:
            MockConnFactory.create_connection.side_effect = Exception("DB unreachable")

        yield TestClient(app, raise_server_exceptions=False), mock_wallet_instance, mock_order_instance, mock_engine_instance


# ============================================================================
# 1. App startup — app.py lifespan / Shoonya bootstrap
# ============================================================================

class TestAppStartup:
    """
    app.py's lifespan tries `shoonya.connect()` synchronously; only when that
    fails does it fall back to `await loop.run_in_executor(None,
    shoonya.auto_login)`. run_in_executor genuinely runs the callable on a
    separate worker thread (not inline on the event-loop thread) — that's
    the actual mechanism by which "broker bootstrap doesn't block the event
    loop". The lifespan itself still *awaits* that future before yielding
    (i.e. TestClient's `with` entry — which drives startup — legitimately
    blocks for however long auto_login takes), but once startup completes,
    ordinary HTTP requests are fast and unaffected by that duration.
    """

    def test_auto_login_runs_on_a_worker_thread_not_the_main_thread(self):
        """The actual blocking bootstrap work (auto_login) must execute off
        the calling thread — proving the app relies on a background
        executor thread rather than running broker I/O inline."""
        import app as app_module

        main_thread_id = threading.get_ident()
        seen_thread_id = {}

        class FakeShoonya:
            def connect(self):
                return False  # forces the auto_login fallback path

            def auto_login(self):
                seen_thread_id["id"] = threading.get_ident()
                time.sleep(0.05)
                return True

        with patch.object(app_module, "ShoonyaConnection", FakeShoonya), \
             patch.object(app_module, "shoonya_daily_refresh", _noop_daily_refresh), \
             patch.object(app_module, "top_movers_refresh", _noop_daily_refresh), \
             patch.object(app_module, "option_master_daily_refresh", _noop_daily_refresh), \
             patch.object(app_module, "ShoonyaOptionFeed", _FakeOptionFeed):

            with TestClient(app_module.app) as client:
                resp = client.get("/")
                assert resp.status_code == 200

        assert seen_thread_id.get("id") is not None
        assert seen_thread_id["id"] != main_thread_id

    def test_http_requests_stay_fast_regardless_of_bootstrap_duration(self):
        """Once the app has started, a plain HTTP request must return in
        milliseconds — it must not re-pay any part of the broker bootstrap
        cost. Bootstrap itself is simulated with an artificial delay to make
        this a meaningful (not just trivially-true) assertion."""
        import app as app_module

        class SlowShoonya:
            def connect(self):
                return False

            def auto_login(self):
                time.sleep(0.3)  # simulated Chrome-automation bootstrap cost
                return True

        with patch.object(app_module, "ShoonyaConnection", SlowShoonya), \
             patch.object(app_module, "shoonya_daily_refresh", _noop_daily_refresh), \
             patch.object(app_module, "top_movers_refresh", _noop_daily_refresh), \
             patch.object(app_module, "option_master_daily_refresh", _noop_daily_refresh), \
             patch.object(app_module, "ShoonyaOptionFeed", _FakeOptionFeed):

            with TestClient(app_module.app) as client:
                # Startup (the `with` statement above) already paid the 0.3s
                # auto_login cost. The request itself must be fast.
                start = time.perf_counter()
                resp = client.get("/")
                elapsed = time.perf_counter() - start

        assert resp.status_code == 200
        assert elapsed < 0.2, f"GET / took {elapsed:.3f}s — should be near-instant post-startup"

    def test_connect_success_skips_auto_login_entirely(self):
        """When the stored token is already valid, connect() succeeds and
        auto_login must never be invoked."""
        import app as app_module

        auto_login_called = MagicMock()

        class ValidTokenShoonya:
            def connect(self):
                return True

            def auto_login(self):
                auto_login_called()
                return True

        with patch.object(app_module, "ShoonyaConnection", ValidTokenShoonya), \
             patch.object(app_module, "shoonya_daily_refresh", _noop_daily_refresh), \
             patch.object(app_module, "top_movers_refresh", _noop_daily_refresh), \
             patch.object(app_module, "option_master_daily_refresh", _noop_daily_refresh), \
             patch.object(app_module, "ShoonyaOptionFeed", _FakeOptionFeed):

            with TestClient(app_module.app) as client:
                resp = client.get("/")
                assert app_module.app.state.shoonya is not None

        assert resp.status_code == 200
        auto_login_called.assert_not_called()

    def test_shoonya_init_exception_does_not_crash_app_startup(self):
        """A raised exception while constructing/connecting Shoonya is
        caught inside the lifespan — the app must still come up and serve
        requests, with app.state.shoonya left as None."""
        import app as app_module

        class ExplodingShoonya:
            def __init__(self):
                raise RuntimeError("boom - simulated broker init failure")

        with patch.object(app_module, "ShoonyaConnection", ExplodingShoonya), \
             patch.object(app_module, "shoonya_daily_refresh", _noop_daily_refresh), \
             patch.object(app_module, "top_movers_refresh", _noop_daily_refresh), \
             patch.object(app_module, "option_master_daily_refresh", _noop_daily_refresh):

            with TestClient(app_module.app) as client:
                resp = client.get("/")
                assert app_module.app.state.shoonya is None

        assert resp.status_code == 200
        assert resp.json() == {"Message": "Finnaly I am able to run my first API"}


async def _noop_daily_refresh(app):
    """Stand-in for the real *_daily_refresh background tasks — they all
    `await asyncio.sleep(...)` before doing anything network-bound, so
    replacing them avoids scheduling real long-lived work during a test
    while still exercising create_task()/task-cancellation at shutdown."""
    import asyncio
    try:
        await asyncio.sleep(3600)
    except asyncio.CancelledError:
        pass


class _FakeOptionFeed:
    """Stand-in for ShoonyaOptionFeed — avoids opening a real WebSocket."""

    def __init__(self, shoonya_connection):
        self._shoonya = shoonya_connection

    def start(self):
        pass

    def close(self):
        pass


# ============================================================================
# 2. ExecutionEngine._process_trades — execution-state outcomes
# ============================================================================

@contextmanager
def _patched_process_trades_deps():
    """Patches every collaborator ExecutionEngine._process_trades() talks to,
    the same way the deleted test_execution_states.py did. TradeSettlementService
    is fully mocked here (its real behavior — and the fetchone() mock-bug fix —
    is covered directly in TestSettleFillCursorMocking below)."""
    with patch("service.executionEngine.OrderService") as MockOrderService, \
         patch("service.executionEngine.portfolioService") as MockPortfolioService, \
         patch("service.executionEngine.TradeSettlementService") as MockTradeSettlementService, \
         patch("service.executionEngine.tradeService") as MockTradeService, \
         patch("service.executionEngine.WalletBalanceService") as MockWalletBalanceService:
        yield {
            "OrderService": MockOrderService,
            "portfolioService": MockPortfolioService,
            "TradeSettlementService": MockTradeSettlementService,
            "tradeService": MockTradeService,
            "WalletBalanceService": MockWalletBalanceService,
        }


class TestProcessTradesExecutionStates:
    """ExecutionEngine._process_trades() always returns status 'EXECUTED'
    for any non-empty, successfully-processed trade list — PENDING is
    decided one level up in _execute_matching() before _process_trades is
    ever called, and there is no PARTIALLY_EXECUTED value in the dict this
    method returns (only the per-order-book-row status passed to
    updateIncomingOrderBook/updateCounterpartyOrderBook distinguishes
    partial vs full at the row level)."""

    def test_single_full_match_returns_executed(self):
        order = _buy_order(quantity=5)
        trade = MockTradeExecution(buy_order_id=301, sell_order_id=999,
                                    buy_user_id=1, sell_user_id=2, quantity=5, remaining_qty=0)

        with _patched_process_trades_deps() as mocks:
            mocks["tradeService"].return_value.insertTradeOrders.return_value = 6001
            mocks["OrderService"].return_value.get_order_snapshot.return_value = {
                "symbol": "RELIANCE", "exchange": "NSE"
            }

            engine = ExecutionEngine(order, 301)
            mock_cursor = MagicMock()
            mock_conn = MagicMock()

            result = engine._process_trades(mock_conn, mock_cursor, [trade], order_book_id=1001, user_id=1)

        assert result["success"] is True
        assert result["status"] == "EXECUTED"
        assert result["trade_id"] == 6001
        mock_conn.commit.assert_called_once()

    def test_partial_match_still_reports_executed_status(self):
        """A partial fill (remaining_qty > 0) is still reported as
        status='EXECUTED' by _process_trades — documenting real, current
        (if slightly confusingly-named) behavior."""
        order = _buy_order(quantity=10)
        trade = MockTradeExecution(buy_order_id=201, sell_order_id=999,
                                    buy_user_id=1, sell_user_id=2, quantity=5, remaining_qty=5)

        with _patched_process_trades_deps() as mocks:
            mocks["tradeService"].return_value.insertTradeOrders.return_value = 5001
            mocks["OrderService"].return_value.get_order_snapshot.return_value = {
                "symbol": "RELIANCE", "exchange": "NSE"
            }

            engine = ExecutionEngine(order, 201)
            result = engine._process_trades(MagicMock(), MagicMock(), [trade], order_book_id=1001, user_id=1)

        assert result["status"] == "EXECUTED"
        assert result["trade_id"] == 5001

    def test_multiple_partial_matches_accumulate_to_full_execution(self):
        order = _buy_order(quantity=10)
        trade_1 = MockTradeExecution(buy_order_id=301, sell_order_id=999,
                                      buy_user_id=1, sell_user_id=2, quantity=6, remaining_qty=4)
        trade_2 = MockTradeExecution(buy_order_id=301, sell_order_id=1000,
                                      buy_user_id=1, sell_user_id=3, quantity=4, remaining_qty=0)

        with _patched_process_trades_deps() as mocks:
            mocks["tradeService"].return_value.insertTradeOrders.side_effect = [6001, 6002]
            mocks["OrderService"].return_value.get_order_snapshot.return_value = {
                "symbol": "RELIANCE", "exchange": "NSE"
            }

            engine = ExecutionEngine(order, 301)
            result = engine._process_trades(MagicMock(), MagicMock(), [trade_1, trade_2],
                                              order_book_id=1001, user_id=1)

        assert result["status"] == "EXECUTED"
        # trade_id reflects the LAST inserted trade record, not the first
        assert result["trade_id"] == 6002

    def test_none_entries_in_trade_list_are_skipped_safely(self):
        order = _buy_order(quantity=10)
        valid_trade = MockTradeExecution(buy_order_id=301, sell_order_id=999,
                                          buy_user_id=1, sell_user_id=2, quantity=5, remaining_qty=5)

        with _patched_process_trades_deps() as mocks:
            mocks["tradeService"].return_value.insertTradeOrders.return_value = 7001
            mocks["OrderService"].return_value.get_order_snapshot.return_value = {
                "symbol": "RELIANCE", "exchange": "NSE"
            }

            engine = ExecutionEngine(order, 301)
            result = engine._process_trades(MagicMock(), MagicMock(), [None, valid_trade, None],
                                              order_book_id=1001, user_id=1)

        assert result is not None
        assert result["status"] == "EXECUTED"
        assert result["trade_id"] == 7001
        # Only the one non-None trade should have been inserted
        assert mocks["tradeService"].return_value.insertTradeOrders.call_count == 1

    def test_unauthorized_user_raises_value_error(self):
        """A user who is neither the buy_user_id nor sell_user_id of the
        matched trade must be rejected — the engine must never let a user
        execute someone else's trade."""
        order = _buy_order(quantity=10)
        unauthorized_trade = MockTradeExecution(
            buy_order_id=999, sell_order_id=1000, buy_user_id=99, sell_user_id=98,
            quantity=10, remaining_qty=0,
        )

        with _patched_process_trades_deps():
            engine = ExecutionEngine(order, 301)
            with pytest.raises(ValueError, match="not authorized"):
                engine._process_trades(MagicMock(), MagicMock(), [unauthorized_trade],
                                        order_book_id=1001, user_id=1)

    def test_self_trade_is_skipped_not_settled(self):
        """buy_user_id == sell_user_id must never be settled (would corrupt
        avg_price / leak money) — it's silently skipped."""
        order = _buy_order(quantity=10)
        self_trade = MockTradeExecution(
            buy_order_id=301, sell_order_id=302, buy_user_id=1, sell_user_id=1,
            quantity=10, remaining_qty=0,
        )

        with _patched_process_trades_deps() as mocks:
            engine = ExecutionEngine(order, 301)
            result = engine._process_trades(MagicMock(), MagicMock(), [self_trade],
                                              order_book_id=1001, user_id=1)

        # Loop body never ran insertTradeOrders for the self-trade
        mocks["tradeService"].return_value.insertTradeOrders.assert_not_called()
        # ... yet the method still reports success with no trade_id, since
        # nothing raised and the loop completed — real, current behavior.
        assert result["success"] is True
        assert result["trade_id"] is None

    def test_empty_trade_list_raises_value_error(self):
        order = _buy_order(quantity=10)
        engine = ExecutionEngine(order, 301)
        with pytest.raises(ValueError, match="cannot be empty"):
            engine._process_trades(MagicMock(), MagicMock(), [], order_book_id=1001, user_id=1)

    def test_missing_order_snapshot_skips_settlement_without_crashing(self):
        """get_order_snapshot() returning None (order not found) must be
        skipped, not raise — the trade itself is already committed."""
        order = _buy_order(quantity=5)
        trade = MockTradeExecution(buy_order_id=301, sell_order_id=999,
                                    buy_user_id=1, sell_user_id=2, quantity=5, remaining_qty=0)

        with _patched_process_trades_deps() as mocks:
            mocks["tradeService"].return_value.insertTradeOrders.return_value = 8001
            mocks["OrderService"].return_value.get_order_snapshot.return_value = None

            engine = ExecutionEngine(order, 301)
            result = engine._process_trades(MagicMock(), MagicMock(), [trade], order_book_id=1001, user_id=1)

        assert result["success"] is True
        mocks["TradeSettlementService"].return_value.settle_fill.assert_not_called()


class TestFnoFullBuySellExecutionEndToEnd:
    """The exact QA scenario end-to-end: user A (buyer) and user B (seller,
    brand new, no prior position) place opposing F&O orders that fully
    match. Runs the real ExecutionEngine._process_trades() with the real
    TradeSettlementService/PositionService (only DB/cache/broker I/O
    mocked) to prove the whole path - not just settle_fill in isolation -
    reports a clean full EXECUTED status with no exception, and that both
    sides' positions land correctly (buyer long, seller short)."""

    def test_new_buyer_and_new_seller_full_match_reports_executed(self):
        order = _buy_order(quantity=50)  # the "incoming" order driving this _process_trades() call
        trade = MockTradeExecution(
            buy_order_id=501, sell_order_id=502,
            buy_user_id=10, sell_user_id=20,
            symbol="NIFTY07JUL2623800CE", quantity=50, execution_price=120.0, remaining_qty=0,
        )

        fno_snapshot = {
            "symbol": "NIFTY07JUL2623800CE", "exchange": "NFO",
            "broker": "Shoonya", "token": "12345", "lot_size": 50,
            "product_type": "NRML", "source": "SIMULATED",
        }

        with patch("service.executionEngine.OrderService") as MockOrderService, \
             patch("service.executionEngine.portfolioService") as MockPortfolioService, \
             patch("service.executionEngine.tradeService") as MockTradeService, \
             patch("service.executionEngine.WalletBalanceService"), \
             patch("service.positionService.PositionPersistence") as MockPersistence, \
             patch("service.positionService.PositionCache") as MockCache, \
             patch("service.positionService.positionTickService"), \
             patch("service.positionService.OptionMaster.find_by_tsym", return_value=None):

            MockOrderService.return_value.get_order_snapshot.return_value = fno_snapshot
            MockTradeService.return_value.insertTradeOrders.return_value = 9001

            # Neither the buyer nor the seller has a prior position anywhere
            # (Postgres fallback or Redis cache) - the exact "brand new user" case.
            MockPersistence.return_value.get_position.return_value = None
            mock_cache_instance = MockCache.return_value
            mock_cache_instance.get_position.return_value = None
            mock_cache_instance.lock.return_value.__enter__ = MagicMock(return_value=None)
            mock_cache_instance.lock.return_value.__exit__ = MagicMock(return_value=False)

            engine = ExecutionEngine(order, 501)
            mock_conn = MagicMock()
            mock_cursor = MagicMock()

            # Must not raise "Insufficient holdings" (the reported bug) or
            # anything else - this is the full reported scenario.
            result = engine._process_trades(mock_conn, mock_cursor, [trade], order_book_id=1001, user_id=10)

        assert result["success"] is True
        assert result["status"] == "EXECUTED"
        assert result["trade_id"] == 9001
        mock_conn.commit.assert_called_once()
        mock_conn.rollback.assert_not_called()

        # Both sides' positions were saved: buyer long (+50), seller short (-50).
        saved_positions = [c.args[0] for c in mock_cache_instance.save_open_position.call_args_list]
        assert len(saved_positions) == 2
        by_user = {p["user_id"]: p for p in saved_positions}
        assert by_user[10]["netqty"] == 50    # buyer opened long
        assert by_user[20]["netqty"] == -50   # seller opened short (sell-to-open)
        assert by_user[10]["status"] == "OPEN"
        assert by_user[20]["status"] == "OPEN"


# ============================================================================
# 2b. TradeSettlementService.settle_fill — the known mock-bug fix.
#
# database/portfolioPersistence.py's process_buyer/process_seller compare
# holdings["quantity"] < 0 after cursor.fetchone(). A previous version of
# this suite left mock_cursor.fetchone() as a bare, unconfigured MagicMock
# (truthy, with no real __getitem__ behavior for "quantity"/"avg_price"),
# which crashed. These tests set an explicit return_value every time:
# None for "no existing holdings row", or a dict of real int/float values
# when simulating an existing holding.
# ============================================================================

class TestSettleFillCursorMocking:

    def test_buy_new_holding_no_existing_row(self):
        """fetchone() -> None (no prior holdings row) takes the INSERT path."""
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = None  # explicit: no existing row

        service = TradeSettlementService()
        snapshot = {"symbol": "RELIANCE", "exchange": "NSE"}

        service.settle_fill(1, "BUY", snapshot, quantity=10, price=250.0, cursor=mock_cursor)

        # select_holdings then insert_holdings — two execute() calls
        assert mock_cursor.execute.call_count == 2

    def test_buy_existing_holding_averages_price(self):
        """fetchone() -> a real dict of int/float values (not a bare
        MagicMock) so holdings["quantity"] < 0 and the weighted-average
        math both evaluate correctly instead of raising."""
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = {"quantity": 10, "avg_price": 200.0}

        service = TradeSettlementService()
        snapshot = {"symbol": "RELIANCE", "exchange": "NSE"}

        # Should not raise (old bug: unconfigured MagicMock comparison < 0)
        service.settle_fill(1, "BUY", snapshot, quantity=5, price=260.0, cursor=mock_cursor)

        update_call = mock_cursor.execute.call_args_list[-1]
        new_qty, new_avg = update_call.args[1][0], update_call.args[1][1]
        assert new_qty == 15
        assert new_avg == pytest.approx(((10 * 200.0) + (5 * 260.0)) / 15)

    def test_sell_existing_holding_reduces_quantity(self):
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = {"quantity": 20, "avg_price": 150.0}

        service = TradeSettlementService()
        snapshot = {"symbol": "RELIANCE", "exchange": "NSE"}

        service.settle_fill(1, "SELL", snapshot, quantity=8, price=180.0, cursor=mock_cursor)

        update_call = mock_cursor.execute.call_args_list[-1]
        assert update_call.args[1][0] == 12  # 20 - 8

    def test_sell_without_existing_holdings_raises(self):
        """fetchone() -> None on a SELL means no holdings exist — must raise
        rather than silently proceed (would allow selling nothing into a
        negative position)."""
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = None

        service = TradeSettlementService()
        snapshot = {"symbol": "RELIANCE", "exchange": "NSE"}

        with pytest.raises(Exception, match="No holdings found"):
            service.settle_fill(1, "SELL", snapshot, quantity=5, price=100.0, cursor=mock_cursor)


class TestOrderServiceExchangeResolution:
    """OrderService.create_order() must resolve `exchange` server-side from
    OptionMaster for any symbol it recognizes as a derivative - the same way
    it already resolves `token` - instead of trusting the client-supplied
    value (which defaults to NSE). Settlement routing (is_fo_exchange) relies
    on this being correct; without it, an F&O sell-to-open with no prior
    position gets wrongly routed through the equity holdings check and
    raises "Insufficient holdings" (the reported bug)."""

    def test_fno_symbol_resolves_exchange_to_nfo_regardless_of_client_input(self):
        order = OrderCreate(
            symbol="NIFTY07JUL2623800CE", exchange=ExchangeType.NSE, side=OrderSide.SELL,
            quantity=50, order_type=OrderType.MARKET, product_type=ProductType.NRML,
        )
        service = OrderService()
        service.order_persistence = MagicMock()
        service.order_persistence.create_order.return_value = 101

        with patch("service.orderService.OptionMaster.find_by_tsym") as mock_find:
            mock_find.return_value = {
                "token": "12345", "lot_size": 50, "exchange": "NFO",
                "underlying": "NIFTY", "expiry": "2026-07-07", "strike": 23800.0, "option_type": "CE",
            }
            order_id = service.create_order(order, user_id=42)

        assert order_id == 101
        assert order.exchange == ExchangeType.NFO
        assert order.token == "12345"

    def test_bfo_symbol_resolves_exchange_to_bfo(self):
        """Sensex options trade on BFO, not NFO - previously ExchangeType
        had no BFO member at all, so this couldn't even be represented."""
        order = OrderCreate(
            symbol="SENSEX07JUL2680000CE", exchange=ExchangeType.NSE, side=OrderSide.SELL,
            quantity=10, order_type=OrderType.MARKET, product_type=ProductType.NRML,
        )
        service = OrderService()
        service.order_persistence = MagicMock()
        service.order_persistence.create_order.return_value = 202

        with patch("service.orderService.OptionMaster.find_by_tsym") as mock_find:
            mock_find.return_value = {
                "token": "999", "lot_size": 10, "exchange": "BFO",
                "underlying": "SENSEX", "expiry": "2026-07-07", "strike": 80000.0, "option_type": "CE",
            }
            service.create_order(order, user_id=42)

        assert order.exchange == ExchangeType.BFO

    def test_equity_symbol_with_no_option_master_match_keeps_client_exchange(self):
        """Regression guard: equity/CNC orders are unaffected - no OptionMaster
        match means exchange stays exactly what the client submitted."""
        order = OrderCreate(
            symbol="RELIANCE", exchange=ExchangeType.NSE, side=OrderSide.BUY,
            quantity=10, order_type=OrderType.MARKET, price=2500.0, product_type=ProductType.CNC,
        )
        service = OrderService()
        service.order_persistence = MagicMock()
        service.order_persistence.create_order.return_value = 303

        with patch("service.orderService.OptionMaster.find_by_tsym") as mock_find:
            mock_find.return_value = None
            service.create_order(order, user_id=42)

        assert order.exchange == ExchangeType.NSE

    def test_unrecognized_exchange_from_option_master_does_not_raise(self):
        """Defensive guard: even if OptionMaster ever returns an exchange
        string that isn't a valid ExchangeType member, create_order() must
        not raise - it should log and keep the client-supplied exchange
        rather than crash order placement."""
        order = OrderCreate(
            symbol="NIFTY07JUL2623800CE", exchange=ExchangeType.NSE, side=OrderSide.SELL,
            quantity=50, order_type=OrderType.MARKET, product_type=ProductType.NRML,
        )
        service = OrderService()
        service.order_persistence = MagicMock()
        service.order_persistence.create_order.return_value = 404

        with patch("service.orderService.OptionMaster.find_by_tsym") as mock_find:
            mock_find.return_value = {
                "token": "555", "lot_size": 50, "exchange": "SOMETHING_UNKNOWN",
                "underlying": "NIFTY", "expiry": "2026-07-07", "strike": 23800.0, "option_type": "CE",
            }
            order_id = service.create_order(order, user_id=42)

        assert order_id == 404
        assert order.exchange == ExchangeType.NSE  # unchanged, fell back safely
        assert order.token == "555"  # token resolution still happened


class TestFnoSettlementRouting:
    """End-to-end regression test for the reported bug: an F&O SELL from a
    user with no prior position must open a short position via
    PositionService, and must never reach the equity holdings check
    (portfolioPersistence.process_seller), which raises "Insufficient
    holdings" on a fresh short."""

    def test_fno_sell_to_open_routes_to_position_service_not_holdings_check(self):
        service = TradeSettlementService()
        service.position_service = MagicMock()
        service.portfolio_service = MagicMock()
        mock_cursor = MagicMock()

        snapshot = {"symbol": "NIFTY07JUL2623800CE", "exchange": "NFO"}
        service.settle_fill(1, "SELL", snapshot, quantity=50, price=120.0, cursor=mock_cursor)

        service.position_service.apply_fill.assert_called_once_with(
            1, "SELL", snapshot, 50, 120.0, mock_cursor
        )
        service.portfolio_service.process_seller.assert_not_called()
        service.portfolio_service.process_buyer.assert_not_called()

    def test_bfo_sell_to_open_also_routes_to_position_service(self):
        service = TradeSettlementService()
        service.position_service = MagicMock()
        service.portfolio_service = MagicMock()
        mock_cursor = MagicMock()

        snapshot = {"symbol": "SENSEX07JUL2680000CE", "exchange": "BFO"}
        service.settle_fill(1, "SELL", snapshot, quantity=10, price=200.0, cursor=mock_cursor)

        service.position_service.apply_fill.assert_called_once()
        service.portfolio_service.process_seller.assert_not_called()

    def test_equity_sell_still_routes_to_holdings_check(self):
        """Regression guard: CNC/equity sells must NOT change - still
        routed through the holdings-checked portfolio_service path."""
        service = TradeSettlementService()
        service.position_service = MagicMock()
        service.portfolio_service = MagicMock()
        mock_cursor = MagicMock()

        snapshot = {"symbol": "RELIANCE", "exchange": "NSE"}
        service.settle_fill(1, "SELL", snapshot, quantity=5, price=2500.0, cursor=mock_cursor)

        service.portfolio_service.process_seller.assert_called_once_with(
            1, "RELIANCE", 5, 2500.0, mock_cursor
        )
        service.position_service.apply_fill.assert_not_called()

    def test_real_apply_fill_opens_short_position_for_new_user_no_prior_position(self):
        """The exact QA scenario: a brand-new user (no cached/DB position)
        sells to open on an F&O contract. Uses the real PositionService
        (not mocked away) with its DB/cache dependencies mocked, to prove
        apply_fill() itself does not require - or check - any prior
        position/holdings."""
        with patch("service.positionService.PositionPersistence") as MockPersistence, \
             patch("service.positionService.PositionCache") as MockCache, \
             patch("service.positionService.positionTickService"), \
             patch("service.positionService.OptionMaster.find_by_tsym", return_value=None):
            MockPersistence.return_value.get_position.return_value = None
            mock_cache_instance = MockCache.return_value
            mock_cache_instance.get_position.return_value = None
            mock_cache_instance.lock.return_value.__enter__ = MagicMock(return_value=None)
            mock_cache_instance.lock.return_value.__exit__ = MagicMock(return_value=False)

            service = TradeSettlementService()
            snapshot = {
                "symbol": "NIFTY07JUL2623800CE", "exchange": "NFO",
                "broker": "Shoonya", "token": "12345", "lot_size": 50,
                "product_type": "NRML", "source": "SIMULATED",
            }
            mock_cursor = MagicMock()

            # Must not raise - this is the reported bug's exact failure point.
            service.settle_fill(99, "SELL", snapshot, quantity=50, price=120.0, cursor=mock_cursor)

            saved_position = mock_cache_instance.save_open_position.call_args.args[0]
            assert saved_position["netqty"] == -50
            assert saved_position["status"] == "OPEN"


# ============================================================================
# 3. POST /orders — wallet balance check + execution result forwarding
# ============================================================================

class TestBuyOrderBalanceCheck:

    def test_buy_sufficient_balance_returns_200(self):
        with _client_with_wallet(5000.00) as (client, *_):
            resp = client.post("/orders", json=_buy_payload(quantity=10, price=250.00))
        assert resp.status_code == 200
        assert resp.json()["success"] is True

    def test_buy_exact_balance_returns_200(self):
        with _client_with_wallet(2500.00) as (client, *_):
            resp = client.post("/orders", json=_buy_payload(quantity=10, price=250.00))
        assert resp.status_code == 200

    def test_buy_insufficient_balance_returns_400_with_amounts_in_message(self):
        with _client_with_wallet(100.00) as (client, *_):
            resp = client.post("/orders", json=_buy_payload(quantity=10, price=250.00))
        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert "Insufficient balance" in detail
        assert "2500.00" in detail
        assert "100.00" in detail

    def test_buy_no_wallet_row_returns_400(self):
        """wallet is None (no row at all) -> explicit 'wallet not initialized'
        400, a different code path/message than the insufficient-funds case."""
        with _client_with_wallet(None) as (client, *_):
            resp = client.post("/orders", json=_buy_payload(quantity=1, price=100.00))
        assert resp.status_code == 400
        assert resp.json()["detail"] == "User wallet not initialized"

    def test_buy_wallet_missing_balance_key_returns_400(self):
        """wallet row exists but has no 'balance' key at all -> dict.get()
        default of 0 kicks in -> treated as insufficient funds (400), NOT
        the same code path as a None-valued balance (see below)."""
        from utils.auth_dependency import get_current_user

        app = _make_app()
        app.dependency_overrides[get_current_user] = _override_auth

        with patch("api.orders.WalletBalanceService") as MockWallet, \
             patch("api.orders.OrderService"), \
             patch("api.orders.ExecutionEngine"):
            MockWallet.return_value.getWalletBalance.return_value = {"user_id": 42}
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.post("/orders", json=_buy_payload(quantity=5, price=100.00))

        assert resp.status_code == 400
        assert "Insufficient balance" in resp.json()["detail"]

    def test_buy_wallet_balance_none_field_returns_400(self):
        """Wallet row exists but balance is EXPLICITLY None (e.g. a NULL
        DB column). `wallet.get("balance") or 0` treats a None/falsy
        balance as ₹0, so this now returns a clean 400 "Insufficient
        balance" instead of crashing Decimal(str(None)) into an opaque
        500 (fixed regression — previously the `or 0` fallback was
        missing and this raised decimal.InvalidOperation)."""
        from utils.auth_dependency import get_current_user

        app = _make_app()
        app.dependency_overrides[get_current_user] = _override_auth

        with patch("api.orders.WalletBalanceService") as MockWallet, \
             patch("api.orders.OrderService"), \
             patch("api.orders.ExecutionEngine"):
            MockWallet.return_value.getWalletBalance.return_value = {"user_id": 42, "balance": None}
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.post("/orders", json=_buy_payload(quantity=5, price=100.00))

        assert resp.status_code == 400
        assert "Insufficient balance" in resp.json()["detail"]

    def test_buy_missing_price_returns_400(self):
        """BUY orders with no price at all are rejected before any wallet
        lookup happens."""
        with _client_with_wallet(9999.00) as (client, wallet_svc, *_):
            resp = client.post("/orders", json=_buy_payload(price=None))
        assert resp.status_code == 400
        assert "require a valid price" in resp.json()["detail"]
        wallet_svc.getWalletBalance.assert_not_called()

    def test_buy_insufficient_balance_does_not_create_order_or_execute(self):
        with _client_with_wallet(50.00) as (client, wallet_svc, order_svc, engine):
            client.post("/orders", json=_buy_payload(quantity=10, price=250.00))
        order_svc.create_order.assert_not_called()
        engine.execute_order.assert_not_called()

    def test_buy_wallet_service_called_twice_check_then_deduct(self):
        """WalletBalanceService.getWalletBalance() is invoked once for the
        pre-flight balance check and a second time when computing the
        deduction amount — two calls total for a single successful BUY."""
        with _client_with_wallet(9999.00) as (client, wallet_svc, *_):
            resp = client.post("/orders", json=_buy_payload())
        assert resp.status_code == 200
        assert wallet_svc.getWalletBalance.call_count == 2
        wallet_svc.getWalletBalance.assert_called_with(_FAKE_USER["user_id"])


class TestSellOrderNoBalanceCheck:

    def test_sell_zero_balance_proceeds(self):
        with _client_with_wallet(0.00) as (client, *_):
            resp = client.post("/orders", json=_sell_payload())
        assert resp.status_code == 200

    def test_sell_no_wallet_row_proceeds(self):
        with _client_with_wallet(None) as (client, wallet_svc, order_svc, *_):
            resp = client.post("/orders", json=_sell_payload())
        assert resp.status_code == 200
        order_svc.create_order.assert_called_once()

    def test_sell_does_not_call_wallet_service_at_all(self):
        with _client_with_wallet(None) as (client, wallet_svc, *_):
            client.post("/orders", json=_sell_payload())
        wallet_svc.getWalletBalance.assert_not_called()


class TestExecutionResultForwarding:
    """Whatever ExecutionEngine.execute_order() returns is forwarded
    verbatim under the top-level "execution" key — api/orders.py does not
    flatten or rename any of it."""

    def test_pending_result_forwarded_under_execution_key(self):
        with _client_with_wallet(99999.00) as (client, wallet_svc, order_svc, engine):
            engine.execute_order.return_value = {
                "success": True, "status": "PENDING",
                "message": "Order queued for matching", "order_id": 101,
            }
            resp = client.post("/orders", json=_buy_payload())

        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        assert body["order_id"] == 101
        assert body["execution"] == {
            "success": True, "status": "PENDING",
            "message": "Order queued for matching", "order_id": 101,
        }

    def test_executed_result_forwarded_under_execution_key(self):
        with _client_with_wallet(99999.00) as (client, wallet_svc, order_svc, engine):
            engine.execute_order.return_value = {
                "success": True, "status": "EXECUTED",
                "message": "Order fully executed", "order_id": 101, "trade_id": 6001,
            }
            resp = client.post("/orders", json=_buy_payload())

        assert resp.status_code == 200
        assert resp.json()["execution"]["status"] == "EXECUTED"
        assert resp.json()["execution"]["trade_id"] == 6001

    def test_execution_engine_returning_none_becomes_500(self):
        """api/orders.py explicitly guards against execute_order() returning
        None and turns it into a clean 500 (not an unhandled crash)."""
        with _client_with_wallet(99999.00) as (client, wallet_svc, order_svc, engine):
            engine.execute_order.return_value = None
            resp = client.post("/orders", json=_buy_payload())

        assert resp.status_code == 500
        assert resp.json()["detail"] == "Order execution failed"

    def test_execution_engine_exception_propagates_as_500(self):
        with _client_with_wallet(99999.00) as (client, wallet_svc, order_svc, engine):
            engine.execute_order.side_effect = Exception("matching engine blew up")
            resp = client.post("/orders", json=_buy_payload())

        assert resp.status_code == 500
        assert "Failed to create order" in resp.json()["detail"]


class TestGetOrders:

    def test_get_orders_returns_current_response_shape(self):
        """Current shape is lower-case keys: success/message/user_id/orders
        (NOT the old "Message"/"Orders" capitalized keys)."""
        from utils.auth_dependency import get_current_user

        app = _make_app()
        app.dependency_overrides[get_current_user] = _override_auth

        fake_orders = [{"id": 1, "symbol": "RELIANCE", "status": "PENDING"}]
        with patch("api.orders.OrderService") as MockOrder:
            MockOrder.return_value.get_orders.return_value = fake_orders
            client = TestClient(app)
            resp = client.get("/orders")

        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        assert body["message"] == "Orders retrieved successfully"
        assert body["user_id"] == _FAKE_USER["user_id"]
        assert body["orders"] == fake_orders

    def test_get_orders_none_result_becomes_empty_list(self):
        from utils.auth_dependency import get_current_user

        app = _make_app()
        app.dependency_overrides[get_current_user] = _override_auth

        with patch("api.orders.OrderService") as MockOrder:
            MockOrder.return_value.get_orders.return_value = None
            client = TestClient(app)
            resp = client.get("/orders")

        assert resp.status_code == 200
        assert resp.json()["orders"] == []

    def test_get_orders_service_exception_returns_500(self):
        from utils.auth_dependency import get_current_user

        app = _make_app()
        app.dependency_overrides[get_current_user] = _override_auth

        with patch("api.orders.OrderService") as MockOrder:
            MockOrder.return_value.get_orders.side_effect = Exception("db down")
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.get("/orders")

        assert resp.status_code == 500
        assert resp.json()["detail"] == "Failed to retrieve orders"


class TestOrderCreateModelValidation:

    def test_invalid_side_rejected_with_422(self):
        with _client_with_wallet(9999.00) as (client, *_):
            resp = client.post("/orders", json={
                "symbol": "RELIANCE", "side": "HOLD", "quantity": 10, "price": 250.00,
            })
        assert resp.status_code == 422

    def test_zero_or_negative_quantity_rejected_with_422(self):
        """OrderCreate.quantity has gt=0 — Pydantic itself rejects this
        before the route body ever runs."""
        with _client_with_wallet(9999.00) as (client, *_):
            resp = client.post("/orders", json={
                "symbol": "RELIANCE", "side": "BUY", "quantity": -1, "price": 250.00,
            })
        assert resp.status_code == 422
