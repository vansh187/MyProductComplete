"""
The Shoonya auto-login must never be what blocks the broker account: a
restart at 01:53 IST once retried every 5 minutes until morning and Shoonya
blocked the account. These tests pin the limits in
marketengine/shoonyaLoginGuard.py and that the refresh loop obeys them.

No browser, network or real .env - the guard's clock and state file are
injected, auto_login is a mock.
"""

import asyncio
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import marketengine.ShoonyaConnection as shoonya_conn_module
from marketengine.ShoonyaConnection import schedule_daily_refresh
from marketengine.shoonyaLoginGuard import (
    DISABLED_BY_CONFIG,
    MAX_ATTEMPTS_PER_DAY,
    AutoLoginGuard,
    classify_login_page,
)
from utils.auth_dependency import get_current_user
from utils.market_hours import IST


class _Clock:
    def __init__(self, *args):
        self.now = datetime(*args, tzinfo=IST)

    def __call__(self):
        return self.now


class _SteppingClock:
    """Moves `step` forward on every reading, but never past `cap` - so a
    test can let time pass backoffs without ever reaching tomorrow's window,
    however fast the loop spins."""

    def __init__(self, start, step, cap):
        self.now, self.step, self.cap = start, step, cap

    def __call__(self):
        self.now = min(self.now + self.step, self.cap)
        return self.now


def _guard(*when, **kwargs):
    clock = _Clock(*when)
    return AutoLoginGuard(clock=clock, enabled=kwargs.pop("enabled", True), **kwargs), clock


# ── When an attempt may start ───────────────────────────────────────────

class TestLoginWindow:

    def test_restart_at_night_waits_for_the_morning_window(self):
        """The incident: restart on Thu 2026-10-08 01:53 IST."""
        guard, _ = _guard(2026, 10, 8, 1, 53)
        assert guard.next_attempt_at() == datetime(2026, 10, 8, 5, 30, tzinfo=IST)

    @pytest.mark.parametrize("when", [
        (2026, 10, 8, 5, 30), (2026, 10, 8, 8, 30), (2026, 10, 8, 22, 0),
        (2026, 10, 10, 11, 0),  # Saturday
    ])
    def test_working_login_is_never_delayed_inside_the_window(self, when):
        guard, _ = _guard(*when)
        assert guard.seconds_until_allowed() == 0

    def test_late_night_waits_for_next_morning(self):
        guard, _ = _guard(2026, 10, 8, 23, 45)
        assert guard.next_attempt_at() == datetime(2026, 10, 9, 5, 30, tzinfo=IST)


class TestFailures:

    def test_failures_before_submit_back_off_and_never_stop(self):
        guard, clock = _guard(2026, 10, 8, 10, 0)
        guard.start_attempt()
        guard.record_failure("browser_unavailable")
        assert guard.halted_reason is None
        assert guard.seconds_until_allowed() == 5 * 60
        guard.start_attempt()
        guard.record_failure("login_page_unavailable")
        assert guard.seconds_until_allowed() == 15 * 60
        assert guard.status()["failed_submits_today"] == 0

    @pytest.mark.parametrize("reason", [
        "account_blocked", "invalid_credentials", "password_change_required",
        "totp_secret_missing", "dependency_missing",
    ])
    def test_account_rejection_or_broken_setup_stops_at_once(self, reason):
        guard, _ = _guard(2026, 10, 8, 10, 0)
        guard.start_attempt()
        guard.record_failure(reason, "page said no")
        assert guard.halted_reason == reason
        assert guard.seconds_until_allowed() is None
        assert guard.status()["last_failure"]["detail"] == "page said no"

    @pytest.mark.parametrize("reason", ["no_auth_code", "totp_rejected", "unknown"])
    def test_failed_submits_pause_until_tomorrow_and_resume_by_themselves(self, reason):
        guard, clock = _guard(2026, 10, 8, 10, 0)
        guard.start_attempt()
        guard.record_failure(reason)
        assert guard.seconds_until_allowed() == 5 * 60  # one more try today
        clock.now = clock.now.replace(hour=10, minute=10)
        guard.start_attempt()
        guard.record_failure(reason)
        assert guard.halted_reason is None  # never a permanent stop
        assert guard.next_attempt_at() == datetime(2026, 10, 9, 5, 30, tzinfo=IST)
        clock.now = datetime(2026, 10, 9, 5, 30, tzinfo=IST)
        assert guard.seconds_until_allowed() == 0

    def test_daily_attempt_cap_waits_for_tomorrow(self):
        guard, _ = _guard(2026, 10, 8, 10, 0)
        for _ in range(MAX_ATTEMPTS_PER_DAY):
            guard.start_attempt()
            guard.record_failure("browser_unavailable")
        assert guard.next_attempt_at() == datetime(2026, 10, 9, 5, 30, tzinfo=IST)

    def test_success_clears_everything(self):
        guard, _ = _guard(2026, 10, 8, 10, 0)
        guard.record_failure("account_blocked")
        guard.record_success()  # e.g. a manual admin login
        assert guard.halted_reason is None
        assert guard.seconds_until_allowed() == 0

    def test_resume_lifts_a_stop(self):
        guard, _ = _guard(2026, 10, 8, 10, 0)
        guard.record_failure("invalid_credentials")
        assert guard.resume() is True
        assert guard.seconds_until_allowed() == 0

    def test_disabled_by_config_never_tries_and_cannot_be_resumed(self):
        guard, _ = _guard(2026, 10, 8, 10, 0, enabled=False)
        assert guard.halted_reason == DISABLED_BY_CONFIG
        assert guard.seconds_until_allowed() is None
        assert guard.resume() is False
        guard.record_success()
        assert guard.halted_reason == DISABLED_BY_CONFIG

    def test_env_switch_disables(self, monkeypatch):
        monkeypatch.setenv("SHOONYA_AUTO_LOGIN", "off")
        assert AutoLoginGuard(clock=_Clock(2026, 10, 8, 10, 0)).halted_reason == DISABLED_BY_CONFIG


class TestStopSurvivesRestart:

    def test_stop_is_reloaded_after_restart(self, tmp_path):
        state = tmp_path / "guard.json"
        guard, _ = _guard(2026, 10, 8, 10, 0, state_path=state)
        guard.record_failure("account_blocked")

        restarted, _ = _guard(2026, 10, 8, 10, 30, state_path=state)
        assert restarted.halted_reason == "account_blocked"
        assert restarted.seconds_until_allowed() is None

    def test_failed_submits_are_remembered_across_restart_the_same_day(self, tmp_path):
        state = tmp_path / "guard.json"
        guard, _ = _guard(2026, 10, 8, 10, 0, state_path=state)
        guard.record_failure("no_auth_code")

        restarted, _ = _guard(2026, 10, 8, 11, 0, state_path=state)
        restarted.record_failure("no_auth_code")
        assert restarted.next_attempt_at() == datetime(2026, 10, 9, 5, 30, tzinfo=IST)

        next_day, _ = _guard(2026, 10, 9, 9, 0, state_path=state)
        assert next_day.seconds_until_allowed() == 0

    def test_resume_and_success_are_saved(self, tmp_path):
        state = tmp_path / "guard.json"
        guard, _ = _guard(2026, 10, 8, 10, 0, state_path=state)
        guard.record_failure("account_blocked")
        guard.resume()
        assert _guard(2026, 10, 8, 10, 0, state_path=state)[0].halted_reason is None

    def test_unreadable_state_allows_only_one_more_failed_submit_today(self, tmp_path):
        state = tmp_path / "guard.json"
        state.write_text("{not json", encoding="utf-8")
        guard, _ = _guard(2026, 10, 8, 10, 0, state_path=state)
        assert guard.seconds_until_allowed() == 0  # login is not interrupted
        guard.record_failure("no_auth_code")
        assert guard.next_attempt_at() == datetime(2026, 10, 9, 5, 30, tzinfo=IST)


class TestLoginPageClassification:

    @pytest.mark.parametrize("text, reason", [
        ("Your account is Blocked. Contact support", "account_blocked"),
        ("User locked due to multiple attempts", "account_blocked"),
        ("Password expired, please change password", "password_change_required"),
        ("Invalid OTP", "totp_rejected"),
        ("Invalid Password", "invalid_credentials"),
        ("Invalid user id or password", "invalid_credentials"),
    ])
    def test_rejections(self, text, reason):
        assert classify_login_page(text) == reason

    @pytest.mark.parametrize("text", [
        "", None, "Login\nUser ID\nPassword\nTOTP\nForgot Password?\nUnblock User", "Loading...",
    ])
    def test_normal_page_text_is_not_a_rejection(self, text):
        assert classify_login_page(text) is None


# ── The refresh loop obeys the guard ────────────────────────────────────

class _FakeAppState:
    pass


class _FakeApp:
    def __init__(self, guard):
        self.state = _FakeAppState()
        self.state.shoonya_login_guard = guard
        self.state.option_feed = None


async def _run_loop_briefly(app, seconds=0.1):
    with patch.object(shoonya_conn_module, "LOGIN_WAIT_POLL_SECS", 0):
        task = asyncio.create_task(schedule_daily_refresh(app))
        await asyncio.sleep(seconds)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


def _disconnected_shoonya(failure):
    shoonya = MagicMock()
    shoonya.is_connected = False
    shoonya.auto_login.return_value = False
    shoonya.last_login_failure = failure
    return shoonya


@pytest.mark.asyncio
async def test_working_login_goes_through_immediately():
    guard, _ = _guard(2026, 10, 8, 8, 30)
    shoonya = _disconnected_shoonya(None)
    shoonya.auto_login.return_value = True
    app = _FakeApp(guard)
    app.state.shoonya_connection = shoonya
    app.state.shoonya = None

    with patch.object(shoonya_conn_module, "_next_refresh_delay", return_value=3600):
        await _run_loop_briefly(app)

    shoonya.auto_login.assert_called_once()
    assert app.state.shoonya is shoonya


@pytest.mark.asyncio
async def test_loop_never_logs_in_outside_the_window():
    guard, _ = _guard(2026, 10, 8, 1, 53)
    shoonya = _disconnected_shoonya(("no_auth_code", ""))
    app = _FakeApp(guard)
    app.state.shoonya_connection = shoonya
    app.state.shoonya = None

    await _run_loop_briefly(app)

    shoonya.auto_login.assert_not_called()


@pytest.mark.asyncio
async def test_blocked_account_gets_exactly_one_attempt():
    guard, _ = _guard(2026, 10, 8, 10, 0)
    shoonya = _disconnected_shoonya(("account_blocked", "Your account is blocked"))
    app = _FakeApp(guard)
    app.state.shoonya_connection = shoonya
    app.state.shoonya = None

    await _run_loop_briefly(app)

    assert shoonya.auto_login.call_count == 1
    assert guard.halted_reason == "account_blocked"


@pytest.mark.asyncio
async def test_failed_submits_pause_the_loop_after_the_daily_limit():
    # Every clock reading moves 1s forward (past the 5-minute backoff within
    # a few hundred loop turns) but stops at 23:00, before tomorrow's window.
    clock = _SteppingClock(datetime(2026, 10, 8, 10, 0, tzinfo=IST), timedelta(seconds=1),
                           cap=datetime(2026, 10, 8, 23, 0, tzinfo=IST))
    guard = AutoLoginGuard(clock=clock, enabled=True)
    shoonya = _disconnected_shoonya(("no_auth_code", ""))
    app = _FakeApp(guard)
    app.state.shoonya_connection = shoonya
    app.state.shoonya = None

    await _run_loop_briefly(app, seconds=0.3)

    assert shoonya.auto_login.call_count == 2
    assert guard.halted_reason is None
    assert guard.next_attempt_at().date().isoformat() == "2026-10-09"


@pytest.mark.asyncio
async def test_stopped_loop_adopts_a_manual_admin_login():
    guard, _ = _guard(2026, 10, 8, 10, 0)
    guard.record_failure("account_blocked")
    shoonya = _disconnected_shoonya(None)
    app = _FakeApp(guard)
    app.state.shoonya_connection = shoonya
    app.state.shoonya = None

    task = asyncio.create_task(schedule_daily_refresh(app))
    with patch.object(shoonya_conn_module, "LOGIN_WAIT_POLL_SECS", 0), \
         patch.object(shoonya_conn_module, "_next_refresh_delay", return_value=3600):
        await asyncio.sleep(0.02)
        shoonya.is_connected = True  # admin OAuth connected the shared instance
        await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    shoonya.auto_login.assert_not_called()
    assert app.state.shoonya is shoonya
    assert guard.halted_reason is None


def test_unknown_failure_shape_is_treated_as_unexplained():
    assert shoonya_conn_module._login_failure_of(MagicMock()) == ("unknown", "")
    assert shoonya_conn_module._login_failure_of(MagicMock(last_login_failure=("totp_rejected", None))) == ("totp_rejected", "")


# ── Admin routes ────────────────────────────────────────────────────────

def _admin_client(guard, monkeypatch):
    from api.admin_shoonya import router
    monkeypatch.delenv("ADMIN_USER_IDS", raising=False)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: {"user_id": 42}
    app.state.shoonya = None
    app.state.shoonya_login_guard = guard
    return TestClient(app)


def test_status_shows_why_auto_login_stopped(monkeypatch):
    guard, _ = _guard(2026, 10, 8, 10, 0)
    guard.record_failure("account_blocked", "Your account is blocked")
    body = _admin_client(guard, monkeypatch).get("/admin/shoonya/status").json()
    assert body["connected"] is False
    assert body["auto_login"]["halted_reason"] == "account_blocked"
    assert body["auto_login"]["next_attempt_at"] is None


def test_resume_endpoint_lifts_the_stop(monkeypatch):
    guard, _ = _guard(2026, 10, 8, 10, 0)
    guard.record_failure("invalid_credentials")
    resp = _admin_client(guard, monkeypatch).post("/admin/shoonya/auto-login/resume")
    assert resp.status_code == 200
    assert guard.halted_reason is None


def test_resume_refused_when_disabled_by_config(monkeypatch):
    guard, _ = _guard(2026, 10, 8, 10, 0, enabled=False)
    resp = _admin_client(guard, monkeypatch).post("/admin/shoonya/auto-login/resume")
    assert resp.status_code == 409
