"""
Shared refresh + staleness logic for the broker scrip-master JSON files
(appconfig/OptionMaster.py, appconfig/FutureMaster.py), so the schedule,
retry and staleness rules live in one place.

Both files have the shape { "<UNDERLYING>": {"expiries": [...], "<expiry>": ...}, ... }
plus a META_KEY entry stamped at refresh time. Staleness is judged from the
content - never the file's mtime, which a git checkout/pull resets to "now"
on a file that may hold last week's contracts.

Deliberately instance-based (no static/class methods or class-level state).
"""

import asyncio
import json
import logging
import os
import time
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable

from utils.market_hours import IST_OFFSET, MARKET_CLOSE_TIME

logger = logging.getLogger(__name__)

META_KEY = "_meta"

DAILY_REFRESH_HOUR_IST = 8
DAILY_REFRESH_MINUTE_IST = 15
REFRESH_RETRY_DELAY_SECS = 600
PERSIST_ATTEMPTS = 3
PERSIST_RETRY_DELAY_SECS = 0.2


def today_ist() -> date:
    return datetime.now(IST_OFFSET).date()


def underlying_items(raw: dict):
    """(underlying, chains) pairs, skipping the meta entry and malformed values."""
    for underlying, chains in raw.items():
        if underlying != META_KEY and isinstance(chains, dict):
            yield underlying, chains


def upcoming_expiry_dates(expiries: list, today: date | None = None, now: datetime | None = None) -> list[str]:
    """
    ISO expiries still tradable, ascending (ISO dates compare correctly as
    strings - no per-item parsing). On an expiry day after the 15:30 close
    that day's contracts are finished, so they are left out and the default
    chain rolls to the next expiry. An explicit `today` (no time of day)
    keeps the whole day.
    """
    if today is None:
        now = now or datetime.now(IST_OFFSET)
        today_iso = now.date().isoformat()
        expired_today = now.time() >= MARKET_CLOSE_TIME
    else:
        today_iso = today.isoformat()
        expired_today = False
    return sorted(
        expiry for expiry in expiries
        if isinstance(expiry, str) and (expiry > today_iso or (expiry == today_iso and not expired_today))
    )


def master_built_on(raw: dict) -> str | None:
    """ISO date the master was downloaded, or None for a file that predates stamping."""
    meta = raw.get(META_KEY) if isinstance(raw, dict) else None
    built_on = meta.get("built_on") if isinstance(meta, dict) else None
    return built_on if isinstance(built_on, str) else None


def is_master_stale(raw: dict, today: date | None = None) -> bool:
    """
    True when the master can't be trusted as today's contract list:
      - empty, or never stamped by a refresh (e.g. the copy in git);
      - downloaded before today (IST) - the embedded stamp, unlike mtime,
        isn't reset to "now" by a git checkout/pull;
      - some underlying has no upcoming expiry left at all - the end state of
        the old bug, where every weekly in a stale file had passed.
    An already-expired contract still being listed is NOT a staleness signal:
    the broker's own master lags (BFO listed 2026-10-01 SENSEX on
    2026-10-04), so a fresh download would otherwise never count as fresh.
    """
    if not isinstance(raw, dict) or not raw:
        return True
    today_iso = (today or today_ist()).isoformat()
    built_on = master_built_on(raw)
    if built_on is None or built_on < today_iso:
        return True
    for _, chains in underlying_items(raw):
        expiries = chains.get("expiries")
        listed = [expiry for expiry in expiries if isinstance(expiry, str)] if isinstance(expiries, list) else []
        if listed and max(listed) < today_iso:
            return True
    return False


class ScripMasterRefresher:

    def __init__(self, name: str, file_path: Path, download: Callable[[], dict],
                 install: Callable[[dict], None], current_master: Callable[[], dict],
                 refresh_hour: int = DAILY_REFRESH_HOUR_IST, refresh_minute: int = DAILY_REFRESH_MINUTE_IST,
                 retry_delay_secs: float = REFRESH_RETRY_DELAY_SECS):
        """
        download: blocking () -> master dict straight from the broker
        install: swaps a master dict into the owning module's memory
        current_master: () -> the owning module's in-memory master right now
        """
        self._name = name
        self._file_path = file_path
        self._download = download
        self._install = install
        self._current_master = current_master
        self._refresh_hour = refresh_hour
        self._refresh_minute = refresh_minute
        self._retry_delay_secs = retry_delay_secs

    def is_stale(self, today: date | None = None) -> bool:
        return is_master_stale(self._current_master(), today)

    def refresh_now(self) -> bool:
        """Download, stamp built_on, install in memory, then persist to disk.
        The downloaded dict is installed directly - never re-read from the
        file - so a failed or contended write can't leave the in-memory
        master empty. Blocking - run it in a worker thread. Never raises.
        Returns True once the fresh master is live in memory."""
        try:
            master = self._download()
        except Exception as exc:
            logger.warning(f"[{self._name}] Download failed: {exc}")
            return False
        if not isinstance(master, dict) or not any(chains.get("expiries") for _, chains in underlying_items(master)):
            logger.warning(f"[{self._name}] Downloaded master has no expiries - keeping the current one")
            return False

        master[META_KEY] = {"built_on": today_ist().isoformat()}
        try:
            self._install(master)
        except Exception as exc:
            logger.error(f"[{self._name}] Installing the downloaded master failed: {exc}", exc_info=True)
            return False
        self._persist(master)
        logger.info(f"[{self._name}] Refreshed scrip master from the broker")
        return True

    def _persist(self, master: dict) -> None:
        """Atomic write: a unique temp file per call (several server workers
        may refresh at once), then os.replace. Retried briefly because on
        Windows the replace fails while another process has the file open.
        A failure only costs a re-download at the next startup - the master
        is already live in memory."""
        tmp_file = self._file_path.with_name(f"{self._file_path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        try:
            tmp_file.write_text(json.dumps(master, ensure_ascii=False), encoding="utf-8")
            for attempt in range(1, PERSIST_ATTEMPTS + 1):
                try:
                    os.replace(tmp_file, self._file_path)
                    return
                except PermissionError:
                    if attempt == PERSIST_ATTEMPTS:
                        raise
                    time.sleep(PERSIST_RETRY_DELAY_SECS)
        except Exception as exc:
            logger.warning(f"[{self._name}] Could not write {self._file_path.name} (in-memory master is current): {exc}")
            try:
                tmp_file.unlink(missing_ok=True)
            except OSError:
                pass

    def seconds_until_next_refresh(self) -> float:
        try:
            now = datetime.now(IST_OFFSET)
            candidate = now.replace(hour=self._refresh_hour, minute=self._refresh_minute, second=0, microsecond=0)
            if now >= candidate:
                candidate += timedelta(days=1)
            return max((candidate - now).total_seconds(), 1.0)
        except Exception:
            return 24 * 3600.0

    async def run_forever(self) -> None:
        """Refreshes immediately when stale (at startup, and again after any
        failed attempt every retry_delay_secs), then daily at the configured
        IST time. Never returns; only cancellation stops it."""
        while True:
            try:
                loop = asyncio.get_running_loop()
                if self.is_stale():
                    if not await loop.run_in_executor(None, self.refresh_now):
                        await asyncio.sleep(self._retry_delay_secs)
                        continue
                await asyncio.sleep(self.seconds_until_next_refresh())
                await loop.run_in_executor(None, self.refresh_now)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(f"[{self._name}] Refresh loop error: {exc}")
                await asyncio.sleep(self._retry_delay_secs)
