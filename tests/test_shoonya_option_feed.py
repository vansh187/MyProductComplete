"""
Unit tests for marketengine/ShoonyaOptionFeed.py, covering: tick handling and
normalization, subscribe/unsubscribe ref-counting, exception-safety of the
detached tick-processing task, and start()/close() socket lifecycle
(closing any prior websocket before opening a new one, surviving errors,
and force-stopping zombie reconnect threads).
"""

import asyncio
import threading
from unittest.mock import MagicMock, patch

import pytest

from marketengine.ShoonyaOptionFeed import ShoonyaOptionFeed, normalize_touchline_tick


def _make_feed():
    shoonya = MagicMock()
    feed = ShoonyaOptionFeed(shoonya)
    return feed, shoonya


# ── normalize_touchline_tick ─────────────────────────────────────────────

class TestNormalizeTouchlineTick:

    def test_maps_known_fields(self):
        raw = {"lp": "123.45", "bp1": "123.0", "sp1": "124.0", "v": "500", "oi": "1000", "poi": "900"}
        result = normalize_touchline_tick(raw)
        assert result == {
            "ltp": 123.45,
            "bid": 123.0,
            "ask": 124.0,
            "volume": 500,
            "oi": 1000,
            "poi": 900,
        }

    def test_only_includes_present_fields(self):
        """Per Shoonya's docs, only t/e/tk are guaranteed on 'tf' updates -
        every other field is present only when it changed."""
        raw = {"t": "tf", "e": "NFO", "tk": "111", "lp": "65.0"}
        result = normalize_touchline_tick(raw)
        assert result == {"ltp": 65.0}

    def test_empty_raw_returns_empty_dict(self):
        assert normalize_touchline_tick({}) == {}

    def test_invalid_numeric_values_do_not_raise(self):
        raw = {"lp": "not-a-number", "v": "also-bad"}
        result = normalize_touchline_tick(raw)
        assert result["ltp"] is None
        assert result["volume"] is None


# ── subscribe/unsubscribe ref-counting ───────────────────────────────────

class TestSubscription:

    def test_ensure_subscribed_calls_broker_for_new_tokens(self):
        feed, shoonya = _make_feed()
        feed.ensure_subscribed({"NFO|111"})
        shoonya._api.subscribe.assert_called_once()
        called_tokens = shoonya._api.subscribe.call_args[0][0]
        assert called_tokens == ["NFO|111"]

    def test_ensure_subscribed_does_not_resubscribe_already_referenced_token(self):
        feed, shoonya = _make_feed()
        feed.ensure_subscribed({"NFO|111"})
        feed.ensure_subscribed({"NFO|111"})
        assert shoonya._api.subscribe.call_count == 1

    def test_ensure_subscribed_noop_when_api_none(self):
        feed, shoonya = _make_feed()
        shoonya._api = None
        feed.ensure_subscribed({"NFO|111"})  # must not raise

    def test_release_unsubscribes_when_refcount_hits_zero(self):
        feed, shoonya = _make_feed()
        feed.ensure_subscribed({"NFO|111"})
        feed.release({"NFO|111"})
        shoonya._api.unsubscribe.assert_called_once()
        assert shoonya._api.unsubscribe.call_args[0][0] == ["NFO|111"]

    def test_release_does_not_unsubscribe_while_still_referenced(self):
        """Two independent subscribers of the same token: releasing one must
        not unsubscribe from the broker while the other still needs it."""
        feed, shoonya = _make_feed()
        feed.ensure_subscribed({"NFO|111"})
        feed.ensure_subscribed({"NFO|111"})  # second reference
        feed.release({"NFO|111"})
        shoonya._api.unsubscribe.assert_not_called()
        assert feed._subscribed_tokens["NFO|111"] == 1

    def test_release_of_unknown_token_does_not_raise(self):
        feed, shoonya = _make_feed()
        feed.release({"NFO|999"})  # never subscribed
        shoonya._api.unsubscribe.assert_called_once()  # still reported as "to remove"


# ── tick exception surfacing ──────────────────────────────────────────────

class TestTickExceptionSurfacing:

    @pytest.mark.asyncio
    async def test_exception_in_tick_handler_is_logged_not_swallowed(self):
        feed, _ = _make_feed()
        feed._async_loop = asyncio.get_running_loop()

        async def _failing_handler(instrument_key, tick):
            raise ValueError("boom")

        feed.on_tick(_failing_handler)

        with patch("marketengine.ShoonyaOptionFeed.logger") as mock_logger:
            feed._on_tick({"t": "tf", "e": "NFO", "tk": "111", "lp": "65.0"})
            # Let the scheduled task (and its done-callback) actually run.
            await asyncio.sleep(0.05)

        assert mock_logger.error.called
        logged_args = mock_logger.error.call_args[0][0]
        assert "Tick handler task failed" in logged_args

    @pytest.mark.asyncio
    async def test_successful_tick_does_not_log_error(self):
        feed, _ = _make_feed()
        feed._async_loop = asyncio.get_running_loop()

        async def _ok_handler(instrument_key, tick):
            pass

        feed.on_tick(_ok_handler)

        with patch("marketengine.ShoonyaOptionFeed.logger") as mock_logger:
            feed._on_tick({"t": "tf", "e": "NFO", "tk": "111", "lp": "65.0"})
            await asyncio.sleep(0.05)

        mock_logger.error.assert_not_called()

    @pytest.mark.asyncio
    async def test_sync_handler_is_called_directly_without_scheduling(self):
        feed, _ = _make_feed()
        feed._async_loop = asyncio.get_running_loop()

        calls = []

        def _sync_handler(instrument_key, tick):
            calls.append((instrument_key, tick))

        feed.on_tick(_sync_handler)
        feed._on_tick({"t": "tf", "e": "NFO", "tk": "111", "lp": "65.0"})

        assert calls == [("NFO|111", {"ltp": 65.0})]

    def test_ignores_non_touchline_message_types(self):
        feed, _ = _make_feed()
        calls = []
        feed.on_tick(lambda k, t: calls.append((k, t)))
        feed._on_tick({"t": "ck", "e": "NFO", "tk": "111"})  # order update, not touchline
        assert calls == []

    def test_ignores_tick_missing_exchange_or_token(self):
        feed, _ = _make_feed()
        calls = []
        feed.on_tick(lambda k, t: calls.append((k, t)))
        feed._on_tick({"t": "tf", "lp": "65.0"})  # no 'e'/'tk'
        assert calls == []

    def test_malformed_tick_does_not_raise(self):
        feed, _ = _make_feed()
        feed._on_tick(None)  # must not raise -- caught by the broad except


# ── start() closes prior socket ───────────────────────────────────────────

class TestStartClosesPriorSocket:

    def test_first_start_does_not_call_close(self):
        """Nothing was ever opened yet - close_websocket() would be pointless."""
        feed, shoonya = _make_feed()

        async def _run():
            feed.start()

        asyncio.run(_run())

        shoonya._api.close_websocket.assert_not_called()
        shoonya._api.start_websocket.assert_called_once()

    def test_second_start_on_same_instance_closes_it_first(self):
        """Internal reconnect (WS-level disconnect, same broker session):
        start() called again on the SAME NorenApi instance must close its
        own previous socket before reopening."""
        feed, shoonya = _make_feed()

        async def _run():
            feed.start()
            feed.start()

        asyncio.run(_run())

        shoonya._api.close_websocket.assert_called_once()
        assert shoonya._api.start_websocket.call_count == 2

    def test_reconnect_with_new_api_instance_closes_the_old_one_not_the_new_one(self):
        """Regression test: ShoonyaConnection.connect() builds a brand new
        NorenApi instance on every token refresh (self._api = _ShoonyaApi(...)).
        The feed must close the OLD instance's socket specifically - calling
        close_websocket() on the NEW instance (which never opened anything)
        would silently leak the old socket/thread forever."""
        feed, shoonya = _make_feed()
        old_api = shoonya._api

        async def _run():
            feed.start()

        asyncio.run(_run())
        old_api.close_websocket.assert_not_called()  # nothing to close yet

        new_api = MagicMock()
        shoonya._api = new_api  # simulates ShoonyaConnection.connect() after a token refresh

        asyncio.run(_run())

        old_api.close_websocket.assert_called_once()   # the orphaned old socket IS closed
        new_api.close_websocket.assert_not_called()    # not the new (never-opened) one
        new_api.start_websocket.assert_called_once()

    def test_start_survives_close_raising_on_reconnect(self):
        """close() on the previous instance may raise (e.g. socket already
        torn down) - that must not prevent start_websocket() on the new one."""
        feed, shoonya = _make_feed()

        async def _run():
            feed.start()

        asyncio.run(_run())
        shoonya._api.close_websocket.side_effect = AttributeError("already closed")

        asyncio.run(_run())  # must not raise

        assert shoonya._api.start_websocket.call_count == 2

    def test_start_noop_when_api_not_connected(self):
        feed, shoonya = _make_feed()
        shoonya._api = None

        async def _run():
            feed.start()

        asyncio.run(_run())  # must not raise

    def test_reconnect_force_stops_zombie_thread_when_close_websocket_is_a_noop(self):
        """Regression test for the production thread-leak bug: NorenApi's own
        close_websocket() silently does nothing once __websocket_connected is
        already False - it never sets the internal stop_event, so the old
        __ws_run_forever thread spins forever and is never joined, leaking
        one OS thread per failed reconnect. start() must force-stop it
        directly instead of trusting close_websocket() alone."""
        feed, shoonya = _make_feed()

        old_api = MagicMock()
        old_api.close_websocket = MagicMock()  # simulates the library's no-op
        old_stop_event = threading.Event()
        old_api._NorenApi__stop_event = old_stop_event

        old_ws = MagicMock()
        old_api._NorenApi__websocket = old_ws

        # A "zombie" thread that only exits once stop_event is actually set -
        # standing in for NorenApi's real __ws_run_forever loop.
        zombie_thread = threading.Thread(target=old_stop_event.wait)
        zombie_thread.daemon = True
        zombie_thread.start()
        old_api._NorenApi__ws_thread = zombie_thread

        feed._api_instance = old_api
        shoonya._api = MagicMock()  # a fresh instance for the new attempt

        async def _run():
            feed.start()

        asyncio.run(_run())

        assert old_stop_event.is_set()
        old_ws.close.assert_called_once()
        zombie_thread.join(timeout=1)
        assert not zombie_thread.is_alive()

    def test_reconnect_flag_cleared_on_fresh_start(self):
        """A fresh external start() (broker reconnect) must supersede
        whatever internal reconnect loop was previously scheduled."""
        feed, shoonya = _make_feed()
        feed._reconnecting = True

        async def _run():
            feed.start()

        asyncio.run(_run())
        assert feed._reconnecting is False


class TestClose:

    def test_close_stops_the_instance_the_socket_was_opened_on(self):
        feed, shoonya = _make_feed()

        async def _run():
            feed.start()

        asyncio.run(_run())
        opened_api = shoonya._api

        # Simulate self._shoonya._api having moved on already (token refresh)
        shoonya._api = MagicMock()

        feed.close()

        opened_api.close_websocket.assert_called_once()

    def test_close_before_any_start_does_not_raise(self):
        feed, shoonya = _make_feed()
        feed.close()  # must not raise


# ── on_open resubscribes after (re)connect ────────────────────────────────

class TestOnOpen:

    def test_on_open_resubscribes_previously_subscribed_tokens(self):
        feed, shoonya = _make_feed()
        feed.ensure_subscribed({"NFO|111", "NFO|222"})
        shoonya._api.subscribe.reset_mock()

        feed._on_open()

        shoonya._api.subscribe.assert_called_once()
        called_tokens = set(shoonya._api.subscribe.call_args[0][0])
        assert called_tokens == {"NFO|111", "NFO|222"}

    def test_on_open_with_no_tokens_does_not_call_subscribe(self):
        feed, shoonya = _make_feed()
        feed._on_open()
        shoonya._api.subscribe.assert_not_called()


# ── on_close / on_error schedule reconnect ─────────────────────────────────

class TestReconnectSignaling:

    def test_on_close_stops_dead_instance_before_the_5s_reconnect_delay(self):
        """Regression test: NorenApi's own __ws_run_forever loop keeps
        retrying the dead connection every 100ms on its own, independent of
        our reconnect timer, until its stop_event is set. _on_close/_on_error
        must signal the old instance to stop immediately, not wait for the
        delayed reconnect."""
        feed, _ = _make_feed()
        feed._async_loop = MagicMock()  # truthy, so _schedule_reconnect proceeds

        dead_api = MagicMock()
        feed._api_instance = dead_api

        with patch.object(feed, "_schedule_reconnect"):
            feed._on_close()

        dead_api.close_websocket.assert_called_once()

    def test_on_error_stops_dead_instance_before_the_5s_reconnect_delay(self):
        feed, _ = _make_feed()
        feed._async_loop = MagicMock()

        dead_api = MagicMock()
        feed._api_instance = dead_api

        with patch.object(feed, "_schedule_reconnect"):
            feed._on_error("boom")

        dead_api.close_websocket.assert_called_once()

    def test_signal_stop_does_not_join_thread(self):
        """_signal_stop_websocket must be safe to call from the WS's own
        thread (which is exactly where _on_close/_on_error run) - it must
        never join, since a thread can't join itself."""
        api = MagicMock()
        thread = MagicMock(spec=threading.Thread)
        api._NorenApi__ws_thread = thread

        ShoonyaOptionFeed._signal_stop_websocket(api)

        thread.join.assert_not_called()

    def test_on_close_does_not_raise_when_schedule_reconnect_fails(self):
        feed, _ = _make_feed()
        feed._async_loop = MagicMock()  # truthy, so _schedule_reconnect proceeds

        with patch.object(feed, "_schedule_reconnect", side_effect=RuntimeError("loop closed")):
            feed._on_close()  # must not raise

    def test_on_error_does_not_raise_when_schedule_reconnect_fails(self):
        feed, _ = _make_feed()
        feed._async_loop = MagicMock()

        with patch.object(feed, "_schedule_reconnect", side_effect=RuntimeError("loop closed")):
            feed._on_error("some error")  # must not raise

    def test_schedule_reconnect_is_idempotent_while_already_reconnecting(self):
        feed, _ = _make_feed()
        feed._async_loop = MagicMock()
        feed._reconnecting = True

        with patch("asyncio.run_coroutine_threadsafe") as mock_schedule:
            feed._schedule_reconnect()

        mock_schedule.assert_not_called()

    def test_schedule_reconnect_noop_without_async_loop(self):
        feed, _ = _make_feed()
        feed._async_loop = None

        with patch("asyncio.run_coroutine_threadsafe") as mock_schedule:
            feed._schedule_reconnect()

        mock_schedule.assert_not_called()
        assert feed._reconnecting is False


# ── reconnect loop retries on failure ───────────────────────────────────────

class TestReconnectLoop:

    @pytest.mark.asyncio
    async def test_reconnect_loop_calls_start_after_delay(self):
        feed, shoonya = _make_feed()
        feed._async_loop = asyncio.get_running_loop()

        with patch("marketengine.ShoonyaOptionFeed.RECONNECT_DELAY_SECS", 0):
            with patch.object(feed, "start") as mock_start:
                await feed._reconnect_loop()

        mock_start.assert_called_once()

    @pytest.mark.asyncio
    async def test_reconnect_loop_reschedules_itself_on_failure(self):
        feed, shoonya = _make_feed()
        feed._async_loop = asyncio.get_running_loop()

        with patch("marketengine.ShoonyaOptionFeed.RECONNECT_DELAY_SECS", 0):
            with patch.object(feed, "start", side_effect=RuntimeError("still down")):
                with patch.object(feed, "_schedule_reconnect") as mock_schedule:
                    await feed._reconnect_loop()

        mock_schedule.assert_called_once()
        assert feed._reconnecting is False
