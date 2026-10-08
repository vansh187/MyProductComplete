"""
Limits for Shoonya's unattended (headless Chrome + TOTP) login.

Every failed login Shoonya sees counts towards its account lockout. A restart
at night once retried every 5 minutes until morning and the account got
blocked. This guard decides when an attempt may start, so that the
auto-login can never be what blocks the account:

  - only inside the daily login window (Shoonya refuses logins overnight);
  - growing waits between consecutive failures, and a daily attempt cap;
  - at most MAX_FAILED_SUBMITS_PER_DAY failed submits a day (well below the
    lockout), then a pause until the next day's window - it resumes by
    itself. Failures before anything was submitted (Chrome, page load)
    never reach Shoonya and only back off.
  - a full stop only when Shoonya says the account itself cannot log in
    (blocked, wrong or expired password) - retrying cannot succeed then.
    A manual login through /admin/shoonya or
    POST /admin/shoonya/auto-login/resume lifts it.
  - stop and today's failed submits are saved to disk, so a restart or
    deploy cannot start a new run of failed logins.

A login that works is never delayed: inside the window an attempt starts
at once, and any success clears all limits.

State is only touched from the event loop thread.
"""

import json
import logging
import os
from datetime import datetime, time, timedelta
from pathlib import Path

from utils.market_hours import IST

logger = logging.getLogger(__name__)

LOGIN_WINDOW_START = time(5, 30)
LOGIN_WINDOW_END = time(23, 30)
BACKOFF_MINUTES = (5, 15, 30, 60)
MAX_ATTEMPTS_PER_DAY = 8
MAX_FAILED_SUBMITS_PER_DAY = 2

STATE_FILE = Path(__file__).parent.parent / ".shoonya_login_guard.json"

DISABLED_BY_CONFIG = "disabled_by_config"

# Shoonya says the account itself cannot log in: no retry can succeed, each
# one is one more failed login towards the lockout.
REJECTED_REASONS = frozenset({
    "account_blocked",
    "invalid_credentials",
    "password_change_required",
})
# Submitted without success but possibly transient (a TOTP that expired in
# transit, a slow redirect): limited per day, never a permanent stop.
FAILED_SUBMIT_REASONS = frozenset({"no_auth_code", "totp_rejected", "unknown"})
# Retrying cannot work until someone fixes the setup.
SETUP_REASONS = frozenset({"totp_secret_missing", "dependency_missing"})

# Error phrases Shoonya's login page shows, checked in this order.
_LOGIN_PAGE_ERRORS = (
    ("account_blocked", ("blocked", "locked", "suspended")),
    ("password_change_required", ("change password", "change your password", "password expired",
                                  "password has expired", "new password")),
    ("totp_rejected", ("invalid otp", "incorrect otp", "wrong otp", "otp is invalid",
                       "invalid totp", "invalid 2fa")),
    ("invalid_credentials", ("invalid password", "incorrect password", "wrong password",
                             "invalid credentials", "invalid user", "invalid login")),
)


def classify_login_page(text: str | None) -> str | None:
    """The rejection reason a login page's visible text shows, or None."""
    lowered = (text or "").lower()
    for reason, phrases in _LOGIN_PAGE_ERRORS:
        if any(phrase in lowered for phrase in phrases):
            return reason
    return None


def _auto_login_enabled_in_env() -> bool:
    return os.getenv("SHOONYA_AUTO_LOGIN", "on").strip().lower() not in ("0", "off", "false", "no")


class AutoLoginGuard:

    def __init__(self, clock=None, enabled: bool | None = None, state_path: Path | None = None):
        self._clock = clock or (lambda: datetime.now(IST))
        self._state_path = state_path
        self._enabled = _auto_login_enabled_in_env() if enabled is None else enabled
        self._halted_reason: str | None = None
        self._failed_submits = 0
        self._failed_submits_day = None
        self._day = None
        self._attempts_today = 0
        self._consecutive_failures = 0
        self._retry_at: datetime | None = None
        self._last_failure: dict | None = None
        self._load()

    @property
    def halted_reason(self) -> str | None:
        return DISABLED_BY_CONFIG if not self._enabled else self._halted_reason

    def seconds_until_allowed(self) -> float | None:
        """0 when an attempt may start now, the wait in seconds otherwise,
        None while stopped (only a manual login or resume() lifts that)."""
        next_at = self.next_attempt_at()
        if next_at is None:
            return None
        return max(0.0, (next_at - self._clock()).total_seconds())

    def next_attempt_at(self) -> datetime | None:
        if self.halted_reason:
            return None
        now = self._clock()
        self._roll_day(now)
        earliest = now
        if self._retry_at is not None and self._retry_at > earliest:
            earliest = self._retry_at
        if self._attempts_today >= MAX_ATTEMPTS_PER_DAY or self._failed_submits_today(now) >= MAX_FAILED_SUBMITS_PER_DAY:
            tomorrow = datetime.combine(now.date() + timedelta(days=1), time.min, tzinfo=now.tzinfo)
            earliest = max(earliest, tomorrow)
        return self._window_open_at_or_after(earliest)

    def start_attempt(self) -> None:
        self._roll_day(self._clock())
        self._attempts_today += 1

    def record_success(self) -> None:
        """A working session from any source (auto-login or a manual admin
        login) proves the account is usable again."""
        had_state = self._halted_reason is not None or self._failed_submits
        self._halted_reason = None
        self._failed_submits = 0
        self._consecutive_failures = 0
        self._retry_at = None
        self._last_failure = None
        if had_state:
            self._save()

    def record_failure(self, reason: str, detail: str = "") -> None:
        now = self._clock()
        self._consecutive_failures += 1
        self._last_failure = {"reason": reason, "detail": detail[:300], "at": now.isoformat(timespec="seconds")}
        if reason in REJECTED_REASONS or reason in SETUP_REASONS:
            self._halted_reason = reason
            self._retry_at = None
            self._save()
            return
        if reason in FAILED_SUBMIT_REASONS:
            self._failed_submits = self._failed_submits_today(now) + 1
            self._failed_submits_day = now.date().isoformat()
            self._save()
        backoff = BACKOFF_MINUTES[min(self._consecutive_failures, len(BACKOFF_MINUTES)) - 1]
        self._retry_at = now + timedelta(minutes=backoff)

    def resume(self) -> bool:
        """Lifts a stop (and today's pause) after the account was fixed. A
        config-disabled auto-login stays off; False then."""
        if not self._enabled:
            return False
        self._halted_reason = None
        self._failed_submits = 0
        self._consecutive_failures = 0
        self._retry_at = None
        self._save()
        return True

    def status(self) -> dict:
        next_at = self.next_attempt_at()
        return {
            "enabled": self._enabled,
            "halted_reason": self.halted_reason,
            "attempts_today": self._attempts_today,
            "max_attempts_per_day": MAX_ATTEMPTS_PER_DAY,
            "failed_submits_today": self._failed_submits_today(self._clock()),
            "max_failed_submits_per_day": MAX_FAILED_SUBMITS_PER_DAY,
            "consecutive_failures": self._consecutive_failures,
            "next_attempt_at": next_at.isoformat(timespec="seconds") if next_at else None,
            "last_failure": self._last_failure,
        }

    def _failed_submits_today(self, now: datetime) -> int:
        return self._failed_submits if self._failed_submits_day == now.date().isoformat() else 0

    def _roll_day(self, now: datetime) -> None:
        if self._day != now.date():
            self._day = now.date()
            self._attempts_today = 0

    def _window_open_at_or_after(self, moment: datetime) -> datetime:
        if moment.time() < LOGIN_WINDOW_START:
            return datetime.combine(moment.date(), LOGIN_WINDOW_START, tzinfo=moment.tzinfo)
        if moment.time() < LOGIN_WINDOW_END:
            return moment
        return datetime.combine(moment.date() + timedelta(days=1), LOGIN_WINDOW_START, tzinfo=moment.tzinfo)

    def _load(self) -> None:
        if self._state_path is None or not self._state_path.exists():
            return
        try:
            state = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            # Unknown history: allow only the last failed submit of today's
            # budget rather than stopping logins altogether.
            logger.error(f"[ShoonyaLoginGuard] unreadable {self._state_path} ({exc}) - assuming one failed login today")
            self._failed_submits = MAX_FAILED_SUBMITS_PER_DAY - 1
            self._failed_submits_day = self._clock().date().isoformat()
            return
        if isinstance(state, dict):
            halted = state.get("halted_reason")
            self._halted_reason = halted if isinstance(halted, str) and halted else None
            failed = state.get("failed_submits")
            self._failed_submits = failed if isinstance(failed, int) and failed > 0 else 0
            day = state.get("failed_submits_day")
            self._failed_submits_day = day if isinstance(day, str) else None

    def _save(self) -> None:
        if self._state_path is None:
            return
        state = {
            "halted_reason": self._halted_reason,
            "failed_submits": self._failed_submits,
            "failed_submits_day": self._failed_submits_day,
        }
        try:
            self._state_path.write_text(json.dumps(state), encoding="utf-8")
        except OSError as exc:
            logger.error(f"[ShoonyaLoginGuard] could not save {self._state_path}: {exc}")


def get_login_guard(app) -> AutoLoginGuard:
    """The one guard per process, shared by the refresh loop and the admin routes."""
    guard = getattr(app.state, "shoonya_login_guard", None)
    if guard is None:
        guard = AutoLoginGuard(state_path=STATE_FILE)
        app.state.shoonya_login_guard = guard
    return guard
