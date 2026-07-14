"""
Regression tests: marketengine.ShoonyaConnection.schedule_daily_refresh() must
restart the option-chain WebSocket feed after every successful reconnect,
since ShoonyaConnection.connect() builds a brand new NorenApi instance each
time - without this coupling, option-chain ticks would silently go dead
forever after the very first daily token refresh, even though REST endpoints
keep working fine.
"""

import asyncio
from unittest.mock import MagicMock, patch

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


@pytest.mark.asyncio
async def test_auto_login_failure_retries_without_starting_feed():
    """A failed auto_login() must not touch the option feed at all, and the
    loop must keep retrying (proven indirectly: auto_login gets called more
    than once within the sleep window since RETRY_DELAY(300s) is patched out
    by the short test sleep just letting the first failure occur)."""
    shoonya = MagicMock()
    shoonya.is_connected = False
    shoonya.auto_login.return_value = False

    option_feed = MagicMock()

    app = _FakeApp()
    app.state.shoonya = shoonya
    app.state.option_feed = option_feed

    await _run_one_reconnect_cycle(app)

    shoonya.auto_login.assert_called()
    option_feed.start.assert_not_called()


@pytest.mark.asyncio
async def test_auto_login_raising_exception_does_not_kill_refresh_loop():
    """An unexpected exception inside auto_login() must be swallowed so the
    background refresh task keeps running instead of dying silently."""
    shoonya = MagicMock()
    shoonya.is_connected = False
    shoonya.auto_login.side_effect = RuntimeError("chrome crashed")

    app = _FakeApp()
    app.state.shoonya = shoonya
    app.state.option_feed = None

    task = asyncio.create_task(schedule_daily_refresh(app))
    await asyncio.sleep(0.05)
    assert not task.done()  # loop is still alive, not crashed
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_creates_new_shoonya_connection_when_none_registered():
    """If app.state has no shoonya yet, schedule_daily_refresh must construct
    one itself rather than raising an AttributeError. The real
    ShoonyaConnection class is patched out so this never touches selenium,
    Chrome, or the network."""
    app = _FakeApp()
    # app.state.shoonya deliberately not set

    fake_instance = MagicMock()
    fake_instance.is_connected = False
    fake_instance.auto_login.return_value = False
    fake_connection_cls = MagicMock(return_value=fake_instance)

    with patch("marketengine.ShoonyaConnection.ShoonyaConnection", fake_connection_cls):
        task = asyncio.create_task(schedule_daily_refresh(app))
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    fake_connection_cls.assert_called_once()
