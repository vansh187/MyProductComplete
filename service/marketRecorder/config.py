"""
Recorder settings, all from the environment (.env) so behaviour changes
need no code change. RECORDER_ENABLED=false (the default) is the kill switch.
load_recorder_config() builds them; RecorderConfig() alone gives defaults.
"""

import logging
import os
import socket
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path

from service.marketRecorder.fileNames import HOLIDAYS_CACHE_NAME

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_DEFAULT_DATA_DIR = _PROJECT_ROOT / "data" / "market"

RECORDER_VERSION = "mpc-recorder 1.0.0"


def _hostname() -> str:
    try:
        return socket.gethostname()
    except Exception:
        return "unknown"


@dataclass
class RecorderConfig:
    enabled: bool = False
    data_dir: Path = _DEFAULT_DATA_DIR
    window_minutes: int = 60
    session_start: time = time(9, 0)
    # The market closes at 15:30; recording runs to 16:00 so closing and
    # post-close updates are captured too (still 7 hourly files a day).
    session_end: time = time(16, 0)
    # Hand-maintained base list (tracked in git) + the list fetched from NSE
    # at runtime (git-ignored, under data_dir) - the calendar uses both.
    holidays_file: Path = _PROJECT_ROOT / "appconfig" / "nse_holidays.json"
    holidays_cache_file: Path = _DEFAULT_DATA_DIR / HOLIDAYS_CACHE_NAME
    refresh_holidays: bool = True   # from NSE's holiday API at start-up and after each close

    underlyings: tuple[str, ...] = ("NIFTY", "BANKNIFTY")
    # "EXCH|TOKEN" -> name, recorded in `ticks` (Nifty 50, Nifty Bank, India VIX).
    index_tokens: dict[str, str] = field(default_factory=lambda: {
        "NSE|26000": "NIFTY", "NSE|26009": "BANKNIFTY", "NSE|26017": "INDIAVIX",
    })
    option_expiries: int = 2
    strikes_each_side: int = 10
    recenter_after_strikes: int = 2
    future_months: int = 2

    queue_max: int = 200_000
    journal_flush_secs: float = 30.0
    chain_snapshot_secs: float = 5.0
    replan_secs: float = 5.0

    # Broker 1-minute candles for every recorded instrument, after the close.
    candles_enabled: bool = True
    candles_calls_per_sec: float = 3.0
    candles_backfill_days: int = 5   # days a missed candle download is retried for
    candles_retry_secs: float = 300.0  # the candle loop re-checks every 5 minutes
    candles_max_attempts: int = 6      # failed attempts before the day is closed as partial/failed

    upload_enabled: bool = False
    upload_at: time = time(16, 15)
    # Today's upload waits for its candles at most this long after upload_at.
    upload_wait_for_candles_mins: int = 45
    drive_root: str = "PrimePipData"
    drive_token_file: Path = Path.home() / ".primepip" / "drive_token.json"
    drive_account: str | None = None   # uploads only go to this Google account
    drive_budget_gb: float = 10.0      # the recorder's own data in Drive
    drive_prune_after_days: int = 60
    local_keep_days: int = 7

    hostname: str = field(default_factory=_hostname)


class _EnvReader:
    """Typed reads of environment variables; a bad value falls back to the default."""

    def text(self, name: str) -> str:
        return (os.getenv(name) or "").strip()

    def flag(self, name: str, default: bool) -> bool:
        value = self.text(name)
        return default if not value else value.lower() in ("1", "true", "yes", "on")

    def integer(self, name: str, default: int) -> int:
        try:
            return int(self.text(name) or default)
        except ValueError:
            logger.warning(f"[Recorder] {name} is not a whole number - using {default}")
            return default

    def number(self, name: str, default: float) -> float:
        try:
            return float(self.text(name) or default)
        except ValueError:
            logger.warning(f"[Recorder] {name} is not a number - using {default}")
            return default

    def clock_time(self, name: str, default: time) -> time:
        value = self.text(name)
        if not value:
            return default
        try:
            hours, minutes = value.split(":")
            return time(int(hours), int(minutes))
        except ValueError:
            logger.warning(f"[Recorder] {name}={value!r} is not HH:MM - using {default:%H:%M}")
            return default

    def path(self, name: str, default: Path) -> Path:
        value = self.text(name)
        return Path(value).expanduser() if value else default


def load_recorder_config() -> RecorderConfig:
    """RecorderConfig from the environment. Never raises: on an unexpected
    error the recorder stays disabled (the safe default)."""
    try:
        env = _EnvReader()
        defaults = RecorderConfig()
        data_dir = env.path("RECORDER_DATA_DIR", defaults.data_dir)
        return RecorderConfig(
            enabled=env.flag("RECORDER_ENABLED", False),
            data_dir=data_dir,
            window_minutes=max(1, env.integer("RECORDER_WINDOW_MINUTES", defaults.window_minutes)),
            session_start=env.clock_time("RECORDER_SESSION_START", defaults.session_start),
            session_end=env.clock_time("RECORDER_SESSION_END", defaults.session_end),
            holidays_file=env.path("RECORDER_HOLIDAYS_FILE", defaults.holidays_file),
            holidays_cache_file=data_dir / HOLIDAYS_CACHE_NAME,
            refresh_holidays=env.flag("RECORDER_REFRESH_HOLIDAYS", True),
            option_expiries=max(1, env.integer("RECORDER_OPTION_EXPIRIES", defaults.option_expiries)),
            strikes_each_side=max(0, env.integer("RECORDER_STRIKES_EACH_SIDE", defaults.strikes_each_side)),
            recenter_after_strikes=max(1, env.integer("RECORDER_RECENTER_AFTER_STRIKES", defaults.recenter_after_strikes)),
            queue_max=max(1000, env.integer("RECORDER_QUEUE_MAX", defaults.queue_max)),
            journal_flush_secs=max(1.0, env.number("RECORDER_JOURNAL_FLUSH_SECS", defaults.journal_flush_secs)),
            chain_snapshot_secs=max(1.0, env.number("RECORDER_CHAIN_SNAPSHOT_SECS", defaults.chain_snapshot_secs)),
            candles_enabled=env.flag("RECORDER_CANDLES_ENABLED", True),
            candles_calls_per_sec=max(0.2, env.number("RECORDER_CANDLES_CALLS_PER_SEC", defaults.candles_calls_per_sec)),
            candles_backfill_days=max(0, env.integer("RECORDER_CANDLES_BACKFILL_DAYS", defaults.candles_backfill_days)),
            candles_retry_secs=max(30.0, env.number("RECORDER_CANDLES_RETRY_SECS", defaults.candles_retry_secs)),
            candles_max_attempts=max(1, env.integer("RECORDER_CANDLES_MAX_ATTEMPTS", defaults.candles_max_attempts)),
            upload_wait_for_candles_mins=max(0, env.integer("RECORDER_UPLOAD_WAIT_FOR_CANDLES_MINS",
                                                            defaults.upload_wait_for_candles_mins)),
            upload_enabled=env.flag("RECORDER_UPLOAD_ENABLED", False),
            upload_at=env.clock_time("RECORDER_UPLOAD_AT", defaults.upload_at),
            drive_root=env.text("RECORDER_DRIVE_ROOT") or defaults.drive_root,
            drive_token_file=env.path("RECORDER_DRIVE_TOKEN_FILE", defaults.drive_token_file),
            drive_account=env.text("RECORDER_DRIVE_ACCOUNT") or None,
            drive_budget_gb=env.number("RECORDER_DRIVE_BUDGET_GB", defaults.drive_budget_gb),
            drive_prune_after_days=env.integer("RECORDER_DRIVE_PRUNE_AFTER_DAYS", defaults.drive_prune_after_days),
            local_keep_days=env.integer("RECORDER_LOCAL_KEEP_DAYS", defaults.local_keep_days),
        )
    except Exception as exc:
        logger.error(f"[Recorder] Could not read recorder settings ({exc!r}) - recorder disabled")
        return RecorderConfig(enabled=False)
