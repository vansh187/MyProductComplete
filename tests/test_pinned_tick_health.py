"""
Index / sector tiles must be served from live ticks, not broker REST.

Seen live on 8 Oct 2026: only INDIAVIX came from ticks; NIFTY, SENSEX,
BANKNIFTY, FINNIFTY and MIDCAP fell back to REST on every rebuild (p95 up to
1.8 s on /api/market/indices, slow first option-chain frame). These pin the
fixes:
- the previous close is remembered for the day (from a full tick or a REST
  answer), so a tick missing it is still complete;
- pinned tokens that go quiet or never sent their full first frame are
  re-subscribed (at most once a minute each, market hours only);
- /api/internal/latency reports per-token tick health;
- plus the two code-review fixes (seed-task cleanup, delta clients when the
  broker is down at connect).
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import api.marketquotes as marketquotes
from marketengine.ShoonyaOptionFeed import ShoonyaOptionFeed
from marketengine.ShoonyaStockFeed import PIN_HEAL_AFTER_SECS, ShoonyaStockFeed


class _SharedFeed:
    is_connected = True

    def __init__(self):
        self.resubscribed: list[set] = []
        self.handler = None

    def on_raw_tick(self, handler):
        self.handler = handler

    def ensure_subscribed(self, tokens):
        pass

    def release(self, tokens):
        pass

    def resubscribe(self, tokens):
        self.resubscribed.append(set(tokens))

    def subscribed_count(self, token):
        return 1


def _stock_feed(market_open=True):
    clock = {"now": 1000.0}
    shared = _SharedFeed()
    feed = ShoonyaStockFeed(shared, clock=lambda: clock["now"], market_open_fn=lambda: market_open)
    return feed, shared, clock


def _frame(token="26000", **fields):
    return {"t": "tf", "e": "NSE", "tk": token, **fields}


class TestPreviousCloseMemory:

    def test_close_from_first_frame_survives_partial_updates(self):
        feed, shared, _ = _stock_feed()
        feed.ingest_raw_tick({**_frame(lp="22200", c="22600"), "t": "tk"})
        feed.ingest_raw_tick(_frame(lp="22210"))
        tick = feed.get_tick("NSE|26000")
        assert tick["ltp"] == 22210.0 and tick["close"] == 22600.0

    def test_remembered_close_completes_a_tick_without_one(self):
        feed, _, _ = _stock_feed()
        feed.ingest_raw_tick(_frame(lp="22210"))  # first full frame was missed
        assert not feed.get_tick("NSE|26000").get("close")

        feed.remember_close("NSE|26000", 22600.0)
        assert feed.get_tick("NSE|26000")["close"] == 22600.0

    def test_yesterdays_close_is_not_used(self):
        feed, _, _ = _stock_feed()
        feed.ingest_raw_tick(_frame(lp="22210"))
        feed._known_close["NSE|26000"] = ("2000-01-01", 22600.0)
        assert not feed.get_tick("NSE|26000").get("close")

    @pytest.mark.parametrize("bad", [None, 0, -5, "", "abc"])
    def test_invalid_close_is_ignored(self, bad):
        feed, _, _ = _stock_feed()
        feed.remember_close("NSE|26000", bad)
        assert "NSE|26000" not in feed._known_close


class TestPinnedTokenHealing:

    def test_pinned_token_without_close_is_resubscribed_once_a_minute(self):
        feed, shared, clock = _stock_feed()
        feed.pin(["NSE|26000"])
        feed.ingest_raw_tick(_frame(lp="22210"))

        assert feed.heal_pinned() == ["NSE|26000"]
        assert shared.resubscribed == [{"NSE|26000"}]
        assert feed.heal_pinned() == []  # not again within the minute
        clock["now"] += PIN_HEAL_AFTER_SECS
        assert feed.heal_pinned() == ["NSE|26000"]

    def test_quiet_pinned_token_is_resubscribed(self):
        feed, shared, clock = _stock_feed()
        feed.pin(["NSE|26000"])
        feed.ingest_raw_tick({**_frame(lp="22200", c="22600"), "t": "tk"})
        assert feed.heal_pinned() == []  # fresh and complete
        clock["now"] += PIN_HEAL_AFTER_SECS + 1
        assert feed.heal_pinned() == ["NSE|26000"]

    def test_never_ticked_pinned_token_is_resubscribed(self):
        feed, shared, _ = _stock_feed()
        feed.pin(["NSE|26000"])
        assert feed.heal_pinned() == ["NSE|26000"]

    def test_nothing_happens_while_market_is_closed(self):
        feed, shared, _ = _stock_feed(market_open=False)
        feed.pin(["NSE|26000"])
        assert feed.heal_pinned() == []
        assert shared.resubscribed == []

    def test_unpinned_tokens_are_left_alone(self):
        feed, shared, _ = _stock_feed()
        feed.touch("NSE|2885")
        assert feed.heal_pinned() == []

    def test_tick_health_report(self):
        feed, _, _ = _stock_feed()
        feed.pin(["NSE|26000", "NSE|26017"])
        feed.ingest_raw_tick({**_frame(token="26017", lp="15.3", c="13.9"), "t": "tk"})
        report = {row["key"]: row for row in feed.tick_health(["NSE|26000", "NSE|26017"])}
        assert report["NSE|26000"]["has_tick"] is False
        assert report["NSE|26017"]["has_tick"] is True
        assert report["NSE|26017"]["close_in_tick"] == 13.9
        assert report["NSE|26017"]["subscriptions"] == 1
        assert report["NSE|26017"]["age_secs"] == 0.0


class TestOptionFeedResubscribe:

    def _feed(self):
        class _InlineSender:
            def submit(self, label, send):
                send()
                return True

            def close(self):
                pass

        shoonya = MagicMock()
        feed = ShoonyaOptionFeed(shoonya, subscription_sender=_InlineSender())
        feed._api_instance = shoonya._api
        feed._socket_open = True
        return feed, shoonya

    def test_resubscribe_resends_only_tokens_still_in_use(self):
        feed, shoonya = self._feed()
        feed.ensure_subscribed({"NSE|26000"})
        shoonya._api.subscribe.reset_mock()

        feed.resubscribe({"NSE|26000", "NSE|99999"})

        shoonya._api.subscribe.assert_called_once_with(["NSE|26000"])
        assert feed.subscribed_count("NSE|26000") == 1  # ref count unchanged
        assert feed.subscribed_count("NSE|99999") == 0


class TestIndicesUseRememberedClose:

    @pytest.mark.asyncio
    async def test_one_rest_answer_makes_the_next_read_tick_served(self):
        feed, _, _ = _stock_feed()
        for key in marketquotes.index_instrument_keys():
            exch, token = key.split("|")
            feed.ingest_raw_tick({"t": "tf", "e": exch, "tk": token, "lp": "100"})  # no close
        shoonya = MagicMock(is_connected=True)
        shoonya.get_index_quote.return_value = {
            "ltp": 100.0, "open": 99.0, "high": 101.0, "low": 98.0, "prev_close": 98.0,
            "change": 2.0, "change_pct": 2.04, "as_of": None,
        }

        rest_used = []
        await marketquotes._fetch_indices(shoonya, None, feed, rest_used)
        assert len(rest_used) == len(marketquotes._ALL_INDICES)

        rest_used = []
        indices, errors = await marketquotes._fetch_indices(shoonya, None, feed, rest_used)
        assert rest_used == [] and errors == []
        assert all(item["as_of"] for item in indices)  # served from ticks now
        assert indices[0]["change"] == 2.0


class TestCodeReviewFixes:

    @pytest.mark.asyncio
    async def test_cancelled_seed_task_does_not_remove_the_next_one(self):
        from service.optionChain.OptionChainService import OptionChainService
        service = OptionChainService(feed=None)
        old_task = asyncio.get_running_loop().create_task(asyncio.sleep(10))
        new_task = asyncio.get_running_loop().create_task(asyncio.sleep(10))
        service._seed_tasks["NIFTY:x"] = new_task  # chain re-created under the same key

        service._forget_seed_task("NIFTY:x", old_task)  # the old task's late callback
        assert service._seed_tasks["NIFTY:x"] is new_task

        service._forget_seed_task("NIFTY:x", new_task)
        assert "NIFTY:x" not in service._seed_tasks
        old_task.cancel()
        new_task.cancel()

    @pytest.mark.asyncio
    async def test_delta_client_gets_cached_chain_when_broker_is_down_at_connect(self):
        import api.optionChain as mod
        cached = {
            "symbol": "NIFTY", "exchange": "NFO", "expiry": "2026-10-13", "spot": 22216.0,
            "strikes": [{"strike": 22200.0, "ce": {"token": "111", "ltp": 90.0}, "pe": None}],
        }
        request = MagicMock()
        request.app.state.shoonya = MagicMock(is_connected=False)
        with patch("api.optionChain.OptionMaster") as mock_master, \
             patch.object(mod._optionChainService, "peek_cached_chain", return_value=(cached, "2026-10-13")):
            mock_master.is_valid_underlying.return_value = True
            response = await mod.stream_option_chain(underlying="nifty", request=request, expiry=None,
                                                     stream_format="delta")
            frames = [json.loads(frame.split("data: ", 1)[1]) async for frame in response.body_iterator]

        assert [frame["t"] for frame in frames] == ["s", "e"]
        assert frames[0]["rows"] == [[22200.0, "111", None, {"token": "111", "ltp": 90.0}, None]]
        assert frames[0]["spot"] == 22216.0
        assert frames[1]["errors"] == [{"reason": "shoonya_disconnected"}]


def test_latency_route_reports_pinned_tick_health(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from api.internal import router
    from utils.auth_dependency import get_current_user

    monkeypatch.delenv("ADMIN_USER_IDS", raising=False)
    feed, _, _ = _stock_feed()
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: {"user_id": 1}
    app.state.stock_feed = feed
    body = TestClient(app).get("/api/internal/latency").json()
    from api.sectorPerformance import sector_instrument_keys
    keys = [row["key"] for row in body["pinned_ticks"]]
    assert len(keys) == len(set(keys))  # one row per token
    assert set(keys) == set(marketquotes.index_instrument_keys()) | set(sector_instrument_keys())
