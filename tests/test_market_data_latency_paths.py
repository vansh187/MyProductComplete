"""
Latency-path regression tests: explore and indices must be served from the
pinned WebSocket tick cache (no broker REST round trip) whenever ticks exist,
REST must only be a bounded fallback, and cached reads must never block on a
refresh in progress.
"""

import asyncio
import threading
from datetime import datetime
import time
from unittest.mock import MagicMock, patch

import pytest

import api.marketquotes as marketquotes
import marketengine.ShoonyaConnection as shoonya_conn_module
from marketengine.ShoonyaConnection import schedule_daily_refresh
from marketengine.shoonyaLoginGuard import AutoLoginGuard
from utils.market_hours import IST
from marketengine.ShoonyaStockFeed import IDLE_TTL_SECS, STALE_TICK_SECS, ShoonyaStockFeed
from service.stocksService import StocksService as stocks_module
from service.stocksService.StocksService import StocksService


WATCHLIST = [
    {"symbol": f"S{i}", "exchange": "NSE", "token": str(1000 + i), "name": f"Stock {i}", "sector": "IT"}
    for i in range(12)
]


class _Watchlist:
    def stocks(self):
        return WATCHLIST


class _Collections:
    def all(self):
        return [{"key": "nifty50", "title": "Nifty 50", "icon_hint": "trending-up"}]


class _TickFeed:
    def __init__(self, ticks: dict):
        self.ticks = ticks

    def get_tick(self, key):
        tick = self.ticks.get(key)
        return dict(tick) if tick else None


def _tick(ltp, close, volume=100):
    return {"ltp": ltp, "close": close, "volume": volume, "open": close, "high": ltp, "low": close}


def _service():
    return StocksService(symbol_master=MagicMock(), explore_watchlist=_Watchlist(), collections_catalog=_Collections())


def _all_ticks():
    return {f"NSE|{s['token']}": _tick(110.0 + i, 100.0, volume=1000 - i) for i, s in enumerate(WATCHLIST)}


# ── ShoonyaStockFeed.pin ────────────────────────────────────────────────

def test_pin_subscribes_once_and_survives_idle_eviction():
    shared = MagicMock()
    feed = ShoonyaStockFeed(shared)

    feed.pin(["NSE|1", "NSE|2"])
    feed.pin(["NSE|1", "NSE|2"])
    feed.touch("NSE|3")

    assert shared.ensure_subscribed.call_count == 2  # one pin batch + one touch
    assert shared.ensure_subscribed.call_args_list[0].args[0] == {"NSE|1", "NSE|2"}

    for key in ("NSE|1", "NSE|3"):
        feed._last_access[key] = time.monotonic() - IDLE_TTL_SECS - 5
    feed._evict_idle_tokens()

    released = shared.release.call_args.args[0]
    assert released == {"NSE|3"}
    assert "NSE|1" in feed._active_tokens


def test_pin_subscribe_failure_is_swallowed():
    shared = MagicMock()
    shared.ensure_subscribed.side_effect = RuntimeError("socket down")
    feed = ShoonyaStockFeed(shared)
    feed.pin(["NSE|1"])  # must not raise
    assert "NSE|1" in feed._pinned_tokens


# ── Explore ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_explore_served_from_ticks_without_rest_calls():
    shoonya = MagicMock()
    shoonya.is_connected = True
    service = _service()

    page = await service.get_explore(shoonya, _TickFeed(_all_ticks()))

    shoonya.get_stock_quote.assert_not_called()
    assert len(page["trending"]) == 10
    assert page["top_gainers"][0]["change_pct"] > 0
    assert page["top_losers"] == []


@pytest.mark.asyncio
async def test_explore_rest_only_for_missing_ticks():
    ticks = _all_ticks()
    del ticks["NSE|1000"]
    del ticks["NSE|1001"]
    shoonya = MagicMock()
    shoonya.is_connected = True
    shoonya.get_stock_quote.return_value = {"ltp": 90.0, "close": 100.0, "volume": 5}
    service = _service()

    page = await service.get_explore(shoonya, _TickFeed(ticks))

    assert shoonya.get_stock_quote.call_count == 2
    assert len(page["top_losers"]) == 2


@pytest.mark.asyncio
async def test_explore_rest_fallback_concurrency_is_capped(monkeypatch):
    monkeypatch.setattr(stocks_module, "_EXPLORE_REST_CONCURRENCY", 3)
    in_flight = 0
    peak = 0

    def slow_quote(_ex, _tk):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        time.sleep(0.02)
        in_flight -= 1
        return {"ltp": 101.0, "close": 100.0, "volume": 1}

    shoonya = MagicMock()
    shoonya.is_connected = True
    shoonya.get_stock_quote.side_effect = slow_quote

    await _service().get_explore(shoonya, _TickFeed({}))

    assert shoonya.get_stock_quote.call_count == len(WATCHLIST)
    assert peak <= 3


@pytest.mark.asyncio
async def test_explore_disconnected_without_ticks_returns_empty_lists():
    shoonya = MagicMock()
    shoonya.is_connected = False
    page = await _service().get_explore(shoonya, None)
    assert page["trending"] == [] and page["collections"]


@pytest.mark.asyncio
async def test_explore_stale_cache_returned_immediately_and_refreshed_in_background(monkeypatch):
    service = _service()
    shoonya = MagicMock()
    shoonya.is_connected = True
    feed = _TickFeed(_all_ticks())

    first = await service.get_explore(shoonya, feed)
    # past the TTL but within the max-stale bound
    service._explore_cache_at = time.monotonic() - (stocks_module._EXPLORE_CACHE_TTL_SECS + 1)

    feed.ticks["NSE|1000"] = _tick(500.0, 100.0, volume=10_000)
    second = await service.get_explore(shoonya, feed)
    assert second is first  # served stale instantly, no waiting on rebuild

    await service._explore_refresh_task
    third = await service.get_explore(shoonya, feed)
    assert third["trending"][0]["ltp"] == 500.0


@pytest.mark.asyncio
async def test_explore_empty_refresh_does_not_overwrite_good_page():
    service = _service()
    shoonya = MagicMock()
    shoonya.is_connected = True
    good = await service.get_explore(shoonya, _TickFeed(_all_ticks()))

    shoonya.is_connected = False
    await service._refresh_explore(shoonya, None)

    assert service._explore_cache_data is good


@pytest.mark.asyncio
async def test_explore_concurrent_cold_calls_build_once():
    service = _service()
    shoonya = MagicMock()
    shoonya.is_connected = True
    calls = 0
    original = service._build_explore_page

    async def counting_build(*args, **kwargs):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        return await original(*args, **kwargs)

    service._build_explore_page = counting_build
    feed = _TickFeed(_all_ticks())
    await asyncio.gather(*[service.get_explore(shoonya, feed) for _ in range(10)])
    assert calls == 1


# ── Indices ─────────────────────────────────────────────────────────────

def _index_ticks():
    return {key: _tick(20000.0 + i, 19900.0) for i, key in enumerate(marketquotes.index_instrument_keys())}


@pytest.fixture(autouse=True)
def _reset_indices_snapshot():
    marketquotes._indices_snapshot["data"] = None
    marketquotes._indices_snapshot["at"] = 0.0
    yield
    marketquotes._indices_snapshot["data"] = None
    marketquotes._indices_snapshot["at"] = 0.0


@pytest.mark.asyncio
async def test_indices_served_from_ticks_without_rest():
    shoonya = MagicMock()
    shoonya.is_connected = True

    indices, errors = await marketquotes._fetch_indices(shoonya, None, _TickFeed(_index_ticks()))

    shoonya.get_index_quote.assert_not_called()
    assert errors == []
    assert len(indices) == len(marketquotes._ALL_INDICES)
    assert indices[0]["change"] == pytest.approx(100.0)
    assert all(i["source"] == "shoonya" for i in indices)


@pytest.mark.asyncio
async def test_indices_rest_fallback_for_missing_tick():
    ticks = _index_ticks()
    missing_key = marketquotes.index_instrument_keys()[0]
    del ticks[missing_key]
    shoonya = MagicMock()
    shoonya.is_connected = True
    shoonya.get_index_quote.return_value = {
        "ltp": 1.0, "open": 1.0, "high": 1.0, "low": 1.0, "prev_close": 1.0,
        "change": 0.0, "change_pct": 0.0, "as_of": None,
    }

    indices, errors = await marketquotes._fetch_indices(shoonya, None, _TickFeed(ticks))

    assert shoonya.get_index_quote.call_count == 1
    assert errors == [] and len(indices) == len(marketquotes._ALL_INDICES)


@pytest.mark.asyncio
async def test_indices_snapshot_coalesces_concurrent_callers():
    shoonya = MagicMock()
    shoonya.is_connected = True
    feed = MagicMock()
    feed.get_tick.side_effect = lambda key: _index_ticks().get(key)

    results = await asyncio.gather(*[
        marketquotes._get_indices_snapshot(shoonya, None, feed) for _ in range(20)
    ])

    assert feed.get_tick.call_count == len(marketquotes._ALL_INDICES)
    assert all(r is results[0] for r in results)


def test_index_tick_with_zero_close_is_not_used():
    idx = marketquotes._ALL_INDICES[0]
    key = f"{idx['shoonya_exchange']}|{idx['shoonya_token']}"
    assert marketquotes._index_quote_from_tick(_TickFeed({key: _tick(100.0, 0.0)}), idx) is None
    assert marketquotes._index_quote_from_tick(None, idx) is None


# ── Late login starts feeds ─────────────────────────────────────────────

class _FakeAppState:
    pass


class _FakeApp:
    def __init__(self):
        self.state = _FakeAppState()
        # Inside the weekday login window, no state file - independent of
        # when the tests run.
        self.state.shoonya_login_guard = AutoLoginGuard(
            clock=lambda: datetime(2026, 10, 8, 10, 0, tzinfo=IST), enabled=True,
        )


async def _run_refresh_briefly(app, seconds=0.05):
    task = asyncio.create_task(schedule_daily_refresh(app))
    await asyncio.sleep(seconds)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_late_login_creates_market_feeds_when_none_exist():
    shoonya = MagicMock()
    shoonya.is_connected = False
    shoonya.auto_login.return_value = True

    app = _FakeApp()
    app.state.shoonya = shoonya
    app.state.option_feed = None
    app.state.create_market_feeds = MagicMock(return_value=True)

    await _run_refresh_briefly(app)

    app.state.create_market_feeds.assert_called_once()


def test_activate_market_feeds_restarts_existing_feed_and_never_raises():
    app = _FakeApp()
    app.state.option_feed = MagicMock()
    app.state.create_market_feeds = MagicMock()
    shoonya_conn_module.activate_market_feeds(app)
    app.state.option_feed.start.assert_called_once()
    app.state.create_market_feeds.assert_not_called()

    app.state.option_feed.start.side_effect = RuntimeError("ws boom")
    shoonya_conn_module.activate_market_feeds(app)  # must not raise


def test_get_or_create_connection_returns_one_shared_instance(monkeypatch):
    created = []

    def factory():
        created.append(MagicMock())
        return created[-1]

    monkeypatch.setattr(shoonya_conn_module, "ShoonyaConnection", factory)
    app = _FakeApp()
    first = shoonya_conn_module.get_or_create_connection(app)
    second = shoonya_conn_module.get_or_create_connection(app)
    assert first is second and len(created) == 1
    assert app.state.shoonya_connection is first


@pytest.mark.asyncio
async def test_refresh_loop_adopts_login_done_by_admin_on_shared_instance():
    """Admin OAuth connects the shared instance while the loop sleeps between
    retries: the loop must adopt it, not run a competing auto_login."""
    shoonya = MagicMock()
    shoonya.is_connected = False

    def failed_login_then_admin_connects():
        shoonya.is_connected = True  # admin OAuth connected the SAME instance meanwhile
        return False

    shoonya.auto_login.side_effect = failed_login_then_admin_connects

    app = _FakeApp()
    app.state.shoonya = None
    app.state.shoonya_connection = shoonya
    app.state.option_feed = None

    with patch.object(shoonya_conn_module, "LOGIN_WAIT_POLL_SECS", 0):
        await _run_refresh_briefly(app)

    assert shoonya.auto_login.call_count == 1  # no second, competing login
    assert app.state.shoonya is shoonya


# ── Stale / dead-socket tick protection ─────────────────────────────────

class _SharedFeed:
    def __init__(self):
        self.is_connected = True
        self.handlers = []

    def on_raw_tick(self, handler):
        self.handlers.append(handler)

    def ensure_subscribed(self, tokens):
        pass

    def release(self, tokens):
        pass


def _stock_feed(market_open=True):
    clock = {"now": 1000.0}
    shared = _SharedFeed()
    feed = ShoonyaStockFeed(shared, clock=lambda: clock["now"], market_open_fn=lambda: market_open)
    feed.ingest_raw_tick({"t": "tk", "e": "NSE", "tk": "1", "lp": "101", "c": "100"})
    return feed, shared, clock


def test_get_tick_refused_while_shared_socket_disconnected():
    feed, shared, _ = _stock_feed()
    assert feed.get_tick("NSE|1")["ltp"] == 101.0
    shared.is_connected = False
    assert feed.get_tick("NSE|1") is None


def test_get_tick_refused_when_stale_during_market_hours():
    feed, _, clock = _stock_feed(market_open=True)
    clock["now"] += STALE_TICK_SECS + 1
    assert feed.get_tick("NSE|1") is None


def test_old_tick_still_served_after_market_close():
    feed, _, clock = _stock_feed(market_open=False)
    clock["now"] += STALE_TICK_SECS * 100
    assert feed.get_tick("NSE|1")["ltp"] == 101.0


@pytest.mark.asyncio
async def test_explore_disconnected_ignores_cached_ticks():
    shoonya = MagicMock()
    shoonya.is_connected = False
    page = await _service().get_explore(shoonya, _TickFeed(_all_ticks()))
    assert page["trending"] == []


@pytest.mark.asyncio
async def test_explore_page_older_than_max_stale_is_rebuilt_not_served():
    service = _service()
    shoonya = MagicMock()
    shoonya.is_connected = True
    feed = _TickFeed(_all_ticks())
    old = await service.get_explore(shoonya, feed)
    service._explore_cache_at = time.monotonic() - (stocks_module._EXPLORE_MAX_STALE_SECS + 1)
    feed.ticks["NSE|1000"] = _tick(900.0, 100.0, volume=99_999)

    fresh = await service.get_explore(shoonya, feed)

    assert fresh is not old
    assert fresh["trending"][0]["ltp"] == 900.0


@pytest.mark.asyncio
async def test_indices_disconnected_do_not_serve_ticks_as_live():
    shoonya = MagicMock()
    shoonya.is_connected = False
    indices, errors = await marketquotes._fetch_indices(shoonya, None, _TickFeed(_index_ticks()))
    assert all(i.get("source") != "shoonya" for i in indices)
    shoonya.get_index_quote.assert_not_called()


# ── Feed restart never blocks the event loop ────────────────────────────

def test_feed_start_does_not_join_old_ws_thread_on_caller_thread():
    from marketengine.ShoonyaOptionFeed import ShoonyaOptionFeed

    shoonya = MagicMock()
    feed = ShoonyaOptionFeed(shoonya)
    old_api = MagicMock()
    stop = threading.Event()
    old_thread = threading.Thread(target=stop.wait, daemon=True)
    old_thread.start()
    old_api._NorenApi__ws_thread = old_thread
    old_api._NorenApi__stop_event = None
    feed._api_instance = old_api

    async def _run():
        started = time.perf_counter()
        feed.start()
        return time.perf_counter() - started

    elapsed = asyncio.run(_run())
    stop.set()
    assert elapsed < 0.5  # the 2s join runs on a helper thread
    assert feed.is_connected is False
    feed._on_open()
    assert feed.is_connected is True
    feed._async_loop = None  # loop above is closed; no reconnect scheduling in this test
    feed._on_close()
    assert feed.is_connected is False
