"""
Unit tests for marketengine/ShoonyaOptionFeed.py, focused on the leak/
exception-safety fixes: exceptions raised inside the detached tick-processing
task must be logged (not silently dropped), and start() must attempt to close
any prior websocket before opening a new one.
"""

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from marketengine.ShoonyaOptionFeed import ShoonyaOptionFeed


def _make_feed():
    shoonya = MagicMock()
    feed = ShoonyaOptionFeed(shoonya)
    return feed, shoonya


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


class TestReconnectCallbacksAreExceptionSafe:

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
