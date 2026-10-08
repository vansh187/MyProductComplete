"""
Market-data speed and correctness (docs/PERF.md):

- OptionChainCache: per-field change tracking, IV only from a tight mid or a
  price traded today, ltp_stale, REST seed never overwrites a live tick.
- ChainBroadcaster: one serialization per flush, slow-client handling.
- OptionChainService: live spot from index ticks; no tick leaks between
  index and option rows.
- Feed normalization: a 0 price is None, never 0.0 (the "CE LTP = spot" bug).
- Indices / sectors / top movers served from ticks, pushed only on change.
- fastjson fallback, stream metrics, /api/internal/latency, /healthz.

No broker, network or DB: every boundary is faked.
"""

import asyncio
import json
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import api.marketquotes as marketquotes
from marketengine.ShoonyaOptionFeed import normalize_touchline_tick
from service.optionChain.ChainBroadcaster import FORMAT_DELTA, FORMAT_FULL, ChainBroadcaster
from service.optionChain.OptionChainCache import OptionChainCache
from service.optionChain.OptionChainService import OptionChainService
from service.sectorPerformance.SectorPerformanceService import SectorIndexRegistry, SectorPerformanceFetcher
from service.topMovers.TopMoversService import StockWatchlist, TopMoversFetcher
from utils.fastjson import JsonEncoder
from utils.streamMetrics import StreamMetrics

EXPIRY = "2099-12-31"
STRIKE_CHAIN = {
    "24300": {"ce_token": "111", "pe_token": "222", "lot_size": 65},
    "24350": {"ce_token": "333", "pe_token": "444", "lot_size": 65},
}


def _cache(expiry: str = EXPIRY) -> OptionChainCache:
    cache = OptionChainCache("NIFTY", expiry, STRIKE_CHAIN)
    cache.set_spot(24270.0)
    return cache


def _week_out_cache() -> OptionChainCache:
    """IV needs a realistic expiry: for a decades-away one no volatility
    prices a near-the-money option at ~62, so IV is rightly None."""
    return _cache((date.today() + timedelta(days=7)).isoformat())


def _leg(cache, strike=24300.0, leg="ce"):
    return next(s for s in cache.get()["strikes"] if s["strike"] == strike)[leg]


class _TickSource:
    def __init__(self, ticks: dict):
        self.ticks = ticks

    def get_tick(self, key):
        tick = self.ticks.get(key)
        return dict(tick) if tick else None

    def received_at_iso(self, key):
        return "2026-10-08T10:15:30.123+05:30" if key in self.ticks else None


# ── OptionChainCache ────────────────────────────────────────────────────

class TestChangeTracking:

    @pytest.mark.asyncio
    async def test_only_changed_fields_are_reported(self):
        cache = _cache()
        await cache.apply_tick("NFO|111", {"ltp": 62.0, "oi": 1000, "volume": 5})
        cache.take_changes()

        await cache.apply_tick("NFO|111", {"ltp": 63.0, "oi": 1000})
        updates, spot_changed, oldest = cache.take_changes()

        assert spot_changed is False and oldest is not None
        assert updates[0][0] == "111"
        assert updates[0][1]["ltp"] == 63.0
        assert "oi" not in updates[0][1]

    @pytest.mark.asyncio
    async def test_identical_tick_is_not_a_change(self):
        cache = _cache()
        await cache.apply_tick("NFO|111", {"ltp": 62.0})
        cache.take_changes()
        await cache.apply_tick("NFO|111", {"ltp": 62.0, "exch_ts": 1_700_000_000_000})
        assert cache.take_changes()[0] == []

    @pytest.mark.asyncio
    async def test_exchange_time_travels_with_a_real_change(self):
        cache = _cache()
        await cache.apply_tick("NFO|111", {"ltp": 62.0, "exch_ts": 1_700_000_000_000})
        fields = cache.take_changes()[0][0][1]
        assert fields["exch_ts"] == 1_700_000_000_000

    def test_spot_change_is_reported(self):
        cache = _cache()
        cache.take_changes()
        cache.set_spot(24300.5)
        assert cache.take_changes()[1] is True
        cache.set_spot(24300.5)  # unchanged
        assert cache.take_changes()[1] is False

    @pytest.mark.asyncio
    async def test_rest_seed_never_overwrites_a_live_tick(self):
        cache = _cache()
        await cache.apply_tick("NFO|111", {"ltp": 70.0})
        cache.seed_leg("24300", "ce", {"ltp": 62.0, "oi": 1000})
        leg = _leg(cache)
        assert leg["ltp"] == 70.0  # live value kept
        assert leg["oi"] == 1000   # missing field filled by the seed


class TestImpliedVolatility:

    @pytest.mark.asyncio
    async def test_iv_from_tight_two_sided_quote(self):
        cache = _week_out_cache()
        await cache.apply_tick("NFO|111", {"ltp": 62.0, "bid": 61.9, "ask": 62.1, "volume": 100})
        assert _leg(cache)["iv"] is not None

    @pytest.mark.asyncio
    async def test_wide_spread_gives_no_iv(self):
        """Illiquid strikes showed IV ~197% from a loose quote."""
        cache = _week_out_cache()
        await cache.apply_tick("NFO|111", {"ltp": 62.0, "bid": 40.0, "ask": 90.0, "volume": 100})
        assert _leg(cache)["iv"] is None

    @pytest.mark.asyncio
    async def test_stale_ltp_without_quote_gives_no_iv(self):
        cache = _week_out_cache()
        await cache.apply_tick("NFO|111", {"ltp": 2556.95, "volume": 0})
        leg = _leg(cache)
        assert leg["iv"] is None
        assert leg["ltp_stale"] is True

    @pytest.mark.asyncio
    async def test_ltp_traded_today_is_used_after_the_close(self):
        cache = _week_out_cache()
        await cache.apply_tick("NFO|111", {"ltp": 62.0, "volume": 1200})
        leg = _leg(cache)
        assert leg["iv"] is not None
        assert leg["ltp_stale"] is False

    @pytest.mark.asyncio
    async def test_iv_change_is_part_of_the_next_batch(self):
        cache = _week_out_cache()
        await cache.apply_tick("NFO|111", {"ltp": 62.0, "bid": 61.9, "ask": 62.1, "volume": 100})
        fields = cache.take_changes()[0][0][1]
        assert "iv" in fields and fields["iv"] is not None


# ── ChainBroadcaster ────────────────────────────────────────────────────

class TestBroadcaster:

    @pytest.mark.asyncio
    async def test_frame_is_serialized_once_for_all_clients(self):
        cache = _cache()
        await cache.apply_tick("NFO|111", {"ltp": 62.0})
        broadcaster = ChainBroadcaster(cache, flush_secs=3600, metrics=StreamMetrics())
        subscribers = [broadcaster.subscribe(FORMAT_FULL) for _ in range(5)]
        for subscriber in subscribers:
            subscriber.queue.get_nowait()  # initial snapshot

        await cache.apply_tick("NFO|111", {"ltp": 63.0})
        broadcaster.flush()

        frames = [subscriber.queue.get_nowait() for subscriber in subscribers]
        assert all(frame is frames[0] for frame in frames)  # the same bytes object
        broadcaster.stop()

    @pytest.mark.asyncio
    async def test_no_change_no_frame(self):
        cache = _cache()
        await cache.apply_tick("NFO|111", {"ltp": 62.0})
        broadcaster = ChainBroadcaster(cache, flush_secs=3600, metrics=StreamMetrics())
        subscriber = broadcaster.subscribe(FORMAT_DELTA)
        subscriber.queue.get_nowait()
        broadcaster.flush()  # drains the pending tick
        while not subscriber.queue.empty():
            subscriber.queue.get_nowait()
        broadcaster.flush()
        assert subscriber.queue.empty()
        broadcaster.stop()

    @pytest.mark.asyncio
    async def test_slow_delta_client_is_resynced_with_a_snapshot(self):
        cache = _cache()
        await cache.apply_tick("NFO|111", {"ltp": 62.0})
        broadcaster = ChainBroadcaster(cache, flush_secs=3600, metrics=StreamMetrics())
        slow = broadcaster.subscribe(FORMAT_DELTA)
        for i in range(20):  # never reads its queue
            await cache.apply_tick("NFO|111", {"ltp": 62.0 + i})
            broadcaster.flush()

        assert slow.dropped >= 1
        frames = []
        while not slow.queue.empty():
            frames.append(json.loads(slow.queue.get_nowait().decode()[6:]))
        broadcaster.flush()
        if not slow.queue.empty():
            frames.append(json.loads(slow.queue.get_nowait().decode()[6:]))
        # After a drop the client never continues on deltas with a gap: the
        # next thing it gets after the overflow is a snapshot.
        assert any(frame["t"] == "s" for frame in frames)
        broadcaster.stop()

    @pytest.mark.asyncio
    async def test_unsubscribe_and_stop(self):
        cache = _cache()
        broadcaster = ChainBroadcaster(cache, flush_secs=0.01, metrics=StreamMetrics())
        subscriber = broadcaster.subscribe(FORMAT_FULL)
        broadcaster.unsubscribe(subscriber)
        broadcaster.unsubscribe(subscriber)  # twice is harmless
        assert broadcaster.subscriber_count == 0
        broadcaster.stop()


# ── OptionChainService routing ──────────────────────────────────────────

class TestTickRouting:

    @pytest.mark.asyncio
    async def test_index_tick_updates_spot_of_that_underlyings_chains_only(self):
        service = OptionChainService(feed=None)
        nifty = OptionChainCache("NIFTY", EXPIRY, STRIKE_CHAIN)
        bank = OptionChainCache("BANKNIFTY", EXPIRY, STRIKE_CHAIN)
        service._caches = {"NIFTY:x": nifty, "BANKNIFTY:x": bank}

        await service._route_tick("NSE|26000", {"ltp": 22220.55})

        assert nifty.spot == 22220.55
        assert bank.spot is None

    @pytest.mark.asyncio
    async def test_index_tick_never_lands_in_an_option_row(self):
        """The '21350 CE LTP = spot' bug: an index value must never be
        written into any option leg."""
        service = OptionChainService(feed=None)
        cache = OptionChainCache("NIFTY", EXPIRY, STRIKE_CHAIN)
        service._caches = {"NIFTY:x": cache}
        service._token_to_cache_keys = {token: {"NIFTY:x"} for token in cache.tokens()}

        await service._route_tick("NSE|26000", {"ltp": 22220.55})
        await service._route_tick("NFO|111", {"ltp": 870.0})

        assert _leg(cache)["ltp"] == 870.0
        assert all(
            leg is None or leg.get("ltp") != 22220.55
            for strike in cache.get()["strikes"] for leg in (strike["ce"], strike["pe"])
        )


class TestFeedNormalization:

    def test_zero_prices_are_none_not_zero(self):
        result = normalize_touchline_tick({"lp": "0.00", "bp1": "0", "sp1": "12.5"})
        assert result == {"ltp": None, "bid": None, "ask": 12.5}

    def test_exchange_time_only_with_real_fields(self):
        assert normalize_touchline_tick({"ft": "1700000000"}) == {}
        assert normalize_touchline_tick({"lp": "10", "ft": "1700000000"})["exch_ts"] == 1_700_000_000_000

    def test_rest_option_quote_missing_price_is_none(self):
        from marketengine.ShoonyaConnection import ShoonyaConnection
        connection = ShoonyaConnection.__new__(ShoonyaConnection)
        connection._connected = True
        connection._api = MagicMock()
        connection._api.get_quotes.return_value = {"stat": "Ok", "lp": "0.00", "bp1": "", "sp1": "5.5", "oi": "10", "v": "0"}
        quote = connection.get_option_quote("NFO", "111")
        assert quote == {"ltp": None, "bid": None, "ask": 5.5, "oi": 10, "volume": 0}


# ── Indices ─────────────────────────────────────────────────────────────

def _index_ticks(offset=0.0) -> dict:
    return {
        f"{idx['shoonya_exchange']}|{idx['shoonya_token']}": {"ltp": 1000.0 + offset, "close": 990.0}
        for idx in marketquotes._ALL_INDICES
    }


@pytest.fixture
def fresh_indices_snapshot(monkeypatch):
    monkeypatch.setattr(marketquotes, "_indices_snapshot",
                        {"at": 0.0, "data": None, "ttl": marketquotes._INDICES_TICK_TTL_SECS})


class TestIndices:

    @pytest.mark.asyncio
    async def test_as_of_is_the_tick_time(self):
        shoonya = MagicMock(is_connected=True)
        indices, _ = await marketquotes._fetch_indices(shoonya, None, _TickSource(_index_ticks()))
        assert all(item["as_of"] == "2026-10-08T10:15:30.123+05:30" for item in indices)

    @pytest.mark.asyncio
    async def test_snapshot_ttl_is_long_when_rest_was_needed(self, fresh_indices_snapshot):
        ticks = _index_ticks()
        del ticks[marketquotes.index_instrument_keys()[0]]
        shoonya = MagicMock(is_connected=True)
        shoonya.get_index_quote.return_value = None
        await marketquotes._get_indices_snapshot(shoonya, None, _TickSource(ticks))
        assert marketquotes._indices_snapshot["ttl"] == marketquotes._INDICES_REST_TTL_SECS

    @pytest.mark.asyncio
    async def test_stream_pushes_on_change_only(self, fresh_indices_snapshot, monkeypatch):
        monkeypatch.setattr(marketquotes, "INDICES_STREAM_INTERVAL_SECS", 0.01)
        monkeypatch.setattr(marketquotes, "INDICES_STREAM_CLOSED_INTERVAL_SECS", 0.01)
        monkeypatch.setattr(marketquotes, "_INDICES_TICK_TTL_SECS", 0.0)
        source = _TickSource(_index_ticks())
        request = MagicMock()
        request.app.state.shoonya = MagicMock(is_connected=True)
        request.app.state.breeze = None
        request.app.state.stock_feed = source

        async def not_disconnected():
            return False
        request.is_disconnected = not_disconnected

        response = await marketquotes.stream_market_indices(request)
        gen = response.body_iterator
        first = json.loads((await asyncio.wait_for(gen.__anext__(), 1.0)).decode()[6:])
        assert first["indices"][0]["value"] == 1000.0

        pending = asyncio.ensure_future(gen.__anext__())
        await asyncio.sleep(0.1)
        assert not pending.done()  # unchanged values: nothing sent

        source.ticks = _index_ticks(offset=5.0)
        second = json.loads((await asyncio.wait_for(pending, 1.0)).decode()[6:])
        assert second["indices"][0]["value"] == 1005.0
        await gen.aclose()


# ── Sectors and top movers from ticks ───────────────────────────────────

class TestSectorsAndTopMovers:

    @pytest.mark.asyncio
    async def test_sectors_from_ticks_make_no_broker_calls(self):
        from api.sectorPerformance import _CONFIG_PATH
        registry = SectorIndexRegistry(_CONFIG_PATH)
        ticks = {key: {"ltp": 110.0, "close": 100.0} for key in registry.instrument_keys()}
        shoonya = MagicMock()
        rest_used = []

        sectors, errors = await SectorPerformanceFetcher(registry).fetch_all(shoonya, _TickSource(ticks), rest_used)

        shoonya.get_index_quote.assert_not_called()
        assert errors == [] and rest_used == []
        assert len(sectors) == len(registry.sectors())
        assert sectors[0]["change_pct"] == 10.0

    @pytest.mark.asyncio
    async def test_sector_without_tick_falls_back_to_rest(self):
        from api.sectorPerformance import _CONFIG_PATH
        registry = SectorIndexRegistry(_CONFIG_PATH)
        keys = registry.instrument_keys()
        ticks = {key: {"ltp": 110.0, "close": 100.0} for key in keys[1:]}
        shoonya = MagicMock()
        shoonya.get_index_quote.return_value = {"ltp": 50.0, "change": 1.0, "change_pct": 2.0}
        rest_used = []

        sectors, _ = await SectorPerformanceFetcher(registry).fetch_all(shoonya, _TickSource(ticks), rest_used)

        assert shoonya.get_index_quote.call_count == 1
        assert rest_used == [registry.sectors()[0]["sector"]]
        assert [s["sector"] for s in sectors] == [s["sector"] for s in registry.sectors()]

    @pytest.mark.asyncio
    async def test_top_movers_from_ticks_make_no_broker_calls(self):
        from api.topMovers import _CONFIG_PATH
        watchlist = StockWatchlist(_CONFIG_PATH)
        ticks = {
            f"{stock['exchange']}|{stock['token']}": {"ltp": 100.0 + i, "close": 100.0}
            for i, stock in enumerate(watchlist.stocks())
        }
        shoonya = MagicMock()
        rest_used = []

        data = await TopMoversFetcher(watchlist, top_n=5).fetch_top_movers(shoonya, True, _TickSource(ticks), rest_used)

        shoonya.get_index_quote.assert_not_called()
        assert rest_used == []
        assert len(data["gainers"]) == 5
        assert data["gainers"][0]["symbol"] == watchlist.stocks()[-1]["symbol"]


# ── Utilities and routes ────────────────────────────────────────────────

class TestUtilities:

    @pytest.mark.parametrize("use_orjson", [True, False])
    def test_encoder_output_is_valid_compact_json(self, use_orjson):
        encoder = JsonEncoder(use_orjson=use_orjson)
        frame = encoder.sse_data({"a": 1.5, "nan": float("nan"), "list": [1, None]})
        assert frame.startswith(b"data: ") and frame.endswith(b"\n\n")
        assert json.loads(frame[6:]) == {"a": 1.5, "nan": None, "list": [1, None]}

    def test_metrics_rates_and_percentiles(self):
        now = [100.0]
        metrics = StreamMetrics(clock=lambda: now[0])
        metrics.subscriber_added("optionchain")
        for i in range(10):
            metrics.record_send("optionchain", 1000)
            metrics.record_recv_to_send("optionchain", float(i * 10))
            now[0] += 1.0
        summary = metrics.snapshot()["optionchain"]
        assert summary["subscribers"] == 1
        assert summary["msgs_per_sec"] == pytest.approx(1.0, rel=0.2)
        assert summary["recv_to_send_ms"]["p95"] == 90.0


def test_latency_route_and_healthz(monkeypatch):
    from api.internal import router
    from utils.auth_dependency import get_current_user
    monkeypatch.delenv("ADMIN_USER_IDS", raising=False)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: {"user_id": 1}
    body = TestClient(app).get("/api/internal/latency").json()
    assert "streams" in body and body["json_backend"] in ("orjson", "json")

    import app as app_module
    with patch.object(app_module.app.state, "shoonya", None, create=True):
        response = TestClient(app_module.app).get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert "X-Process-Time" in response.headers
