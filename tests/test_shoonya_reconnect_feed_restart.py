"""
Regression test: marketengine.ShoonyaConnection.schedule_daily_refresh() must
restart the option-chain WebSocket feed after every successful reconnect,
since ShoonyaConnection.connect() builds a brand new NorenApi instance each
time - without this coupling, option-chain ticks would silently go dead
forever after the very first daily token refresh, even though REST endpoints
keep working fine.
"""

import asyncio
from unittest.mock import MagicMock

import pytest

from marketengine.ShoonyaConnection import schedule_daily_refresh


class _FakeAppState:
    pass


class _FakeApp:
    def __init__(self):
        self.state = _FakeAppState()


async def _run_one_reconnect_cycle(app):
    task = asyncio.create_task(schedule_daily_refresh(app))
    await asyncio.sleep(0.05)  # let the retry-connect branch run once
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_option_feed_restarted_after_successful_reconnect():
    shoonya = MagicMock()
    shoonya.is_connected = False
    shoonya.auto_login.return_value = True

    option_feed = MagicMock()

    app = _FakeApp()
    app.state.shoonya = shoonya
    app.state.option_feed = option_feed

    await _run_one_reconnect_cycle(app)

    option_feed.start.assert_called_once()


@pytest.mark.asyncio
async def test_no_option_feed_registered_does_not_raise():
    """When Shoonya connects before the option feed exists (or the feed
    failed to initialize at startup), reconnect handling must not crash."""
    shoonya = MagicMock()
    shoonya.is_connected = False
    shoonya.auto_login.return_value = True

    app = _FakeApp()
    app.state.shoonya = shoonya
    app.state.option_feed = None

    await _run_one_reconnect_cycle(app)  # must not raise


@pytest.mark.asyncio
async def test_option_feed_start_exception_does_not_kill_refresh_loop():
    shoonya = MagicMock()
    shoonya.is_connected = False
    shoonya.auto_login.return_value = True

    option_feed = MagicMock()
    option_feed.start.side_effect = RuntimeError("websocket boom")

    app = _FakeApp()
    app.state.shoonya = shoonya
    app.state.option_feed = option_feed

    await _run_one_reconnect_cycle(app)  # must not raise / must not crash the task

    option_feed.start.assert_called_once()
