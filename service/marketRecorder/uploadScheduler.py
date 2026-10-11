"""
Uploads recorded days to Google Drive once a day after the close
(RECORDER_UPLOAD_AT, default 16:15 IST), so the whole day is in Drive the
same evening and the next session starts clean.

Today is uploaded only once its session is really closed: past the session
end (plus a margin), AND its last window's file written (or, if the recorder
wasn't running then, SESSION_CLOSE_FALLBACK after the end) - whatever
RECORDER_UPLOAD_AT says. Candles that finish after the upload (broker down
at the close) are uploaded on a later run, with the updated manifest.

On startup it first catches up: any finished day that is still not in the
upload ledger (VM was down at 16:15, Drive was unreachable, ...) is uploaded
then. After each run: local retention, then the Drive budget check.

Uploads are slow network I/O, so they run on a daemon thread of their own:
they never take workers from the broker executor, and they never hold up a
process shutdown - close() sets a stop event that ends retry waits at once
and stops between files; the ledger lets the next run resume.
"""

import asyncio
import logging
import threading
import time
from datetime import datetime, timedelta

from service.marketRecorder.config import RECORDER_VERSION, RecorderConfig
from service.marketRecorder.dayUploader import DayUploader
from service.marketRecorder.driveClient import DriveClient, UploadCancelled
from service.marketRecorder.fileNames import CANDLES_MARKER_NAME
from service.marketRecorder.tradingCalendar import TradingCalendar, day_folder_name
from utils.market_hours import IST

logger = logging.getLogger(__name__)

RETRY_SOON_SECS = 300   # re-check while today's upload waits for its candles
SESSION_CLOSE_MARGIN = timedelta(minutes=5)     # after the session end, before today can upload
SESSION_CLOSE_FALLBACK = timedelta(hours=2)     # last window never written (recorder down then)
STATUS_CACHE_SECS = 60.0


class UploadScheduler:

    def __init__(self, config: RecorderConfig, uploader: DayUploader | None = None):
        self._config = config
        self._stop = threading.Event()
        self.uploader = uploader or DayUploader(
            config.data_dir, config.drive_root,
            lambda: DriveClient(config.drive_token_file, config.drive_account, stop_event=self._stop),
            RECORDER_VERSION, stop_event=self._stop,
        )
        self._calendar = TradingCalendar(config.session_start, config.session_end, config.window_minutes)
        self._running = threading.Lock()
        self._retry_soon = False   # today's upload is waiting for its candles
        self._backlog_cache: tuple[float, list[str]] | None = None
        self.last_run: dict | None = None
        self.next_run_at: str | None = None

    def _session_closed(self, day, now: datetime) -> bool:
        """The day's recording is finished and every window file written."""
        try:
            windows = self._calendar.windows(day)
            if not windows:
                return True
            end = windows[-1].end
            if now < end + SESSION_CLOSE_MARGIN:
                return False
            last_events = self._config.data_dir / day_folder_name(day) / "events" / windows[-1].file_name
            return last_events.is_file() or now >= end + SESSION_CLOSE_FALLBACK
        except Exception as exc:
            logger.warning(f"[Recorder] Session-closed check failed: {exc!r}")
            return False

    def _next_upload_time(self, now: datetime) -> datetime:
        target = datetime.combine(now.date(), self._config.upload_at, IST)
        return target if target > now else target + timedelta(days=1)

    def _log_startup(self) -> None:
        """INFO: where uploads go and whether the Drive login is in place. Never raises."""
        try:
            config = self._config
            token = config.drive_token_file
            logger.info(f"[Drive] Upload scheduler: daily at {config.upload_at:%H:%M} IST to Drive folder "
                        f"'{config.drive_root}' of {config.drive_account or 'ANY account (RECORDER_DRIVE_ACCOUNT not set)'}, "
                        f"keep {config.local_keep_days} days locally, Drive budget {config.drive_budget_gb:g} GB")
            if token.is_file():
                logger.info(f"[Drive] Login token found: {token}")
            else:
                logger.error(f"[Drive] Login token NOT found at {token} - uploads will fail until it is copied there "
                             "(scripts/recorder_drive_login.py), the recorded data stays on disk")
        except Exception as exc:
            logger.warning(f"[Drive] Could not log the upload settings: {exc!r}")

    async def run_forever(self) -> None:
        self._log_startup()
        await self.run_once()   # catch-up on startup
        while not self._stop.is_set():
            try:
                now = datetime.now(IST)
                if self._retry_soon:
                    self._retry_soon = False
                    target = now + timedelta(seconds=RETRY_SOON_SECS)
                else:
                    target = self._next_upload_time(now)
                    logger.info(f"[Drive] Next upload run at {target:%a %d-%m-%Y %H:%M} IST")
                self.next_run_at = target.isoformat(timespec="minutes")
                await asyncio.sleep(max(1.0, (target - now).total_seconds()))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(f"[Recorder] Upload scheduling failed: {exc!r}")
                await asyncio.sleep(60)
                continue
            await self.run_once()

    async def run_once(self) -> dict:
        """Runs one upload pass on a daemon thread and waits for it."""
        loop = asyncio.get_running_loop()
        future = loop.create_future()

        def _finish(result: dict) -> None:
            if not future.done():
                future.set_result(result)

        def _worker() -> None:
            try:
                result = self._run_blocking()
            except Exception as exc:
                logger.error(f"[Recorder] Upload run failed: {exc!r}", exc_info=exc)
                result = {"status": "failed", "error": repr(exc)[:500]}
            try:
                loop.call_soon_threadsafe(_finish, result)
            except RuntimeError:
                pass   # the event loop is gone (shutdown)

        try:
            threading.Thread(target=_worker, name="recorder-upload", daemon=True).start()
            result = await future
        except asyncio.CancelledError:
            self._stop.set()
            raise
        except Exception as exc:
            logger.error(f"[Recorder] Could not run the upload: {exc!r}")
            result = {"status": "failed", "error": repr(exc)[:500]}
        result["at"] = datetime.now(IST).isoformat(timespec="seconds")
        self.last_run = result
        return result

    def _ready(self, now: datetime):
        """Holds today's upload until its broker candles are downloaded (the
        recorder writes .candles_done.json), for at most
        upload_wait_for_candles_mins after upload_at. Earlier days are never held."""
        today = now.date()
        give_up_at = datetime.combine(today, self._config.upload_at, IST) + \
            timedelta(minutes=self._config.upload_wait_for_candles_mins)

        def ready(day) -> bool:
            try:
                if day == today and not self._session_closed(day, now):
                    return False
                if not self._config.candles_enabled or day != today or now >= give_up_at:
                    return True
                return (self._config.data_dir / day_folder_name(day) / CANDLES_MARKER_NAME).is_file()
            except Exception:
                return False
        return ready

    def _today_unsettled(self, today, now: datetime) -> bool:
        """Today was recorded but isn't fully in Drive yet: not uploaded, or
        uploaded before its candle download finished. Re-check every few
        minutes instead of waiting for tomorrow's run."""
        try:
            if today not in self.uploader.local_days():
                return False
            name = day_folder_name(today)
            if not self.uploader.ledger.day_done(name):
                return True
            if not self._config.candles_enabled:
                return False
            marker = self._config.data_dir / name / CANDLES_MARKER_NAME
            return not marker.is_file() or self.uploader.has_late_candles(today)
        except Exception:
            return False

    def _run_blocking(self) -> dict:
        if not self._running.acquire(blocking=False):
            return {"status": "skipped", "reason": "an upload is already running"}
        try:
            now = datetime.now(IST)
            today = now.date()
            upload_time = now.time() >= self._config.upload_at
            days = self.uploader.pending_days(finished_before=today, include_today=today if upload_time else None,
                                              ready=self._ready(now))
            if upload_time and today not in days and self._today_unsettled(today, now):
                # Today is waiting for its session close or candles: check again shortly.
                self._retry_soon = True
                logger.info(f"[Drive] Today's upload is waiting: {self._today_wait_reason(today, now)} - "
                            f"checking again in {RETRY_SOON_SECS // 60} min")
            self._backlog_cache = None
            if days:
                logger.info(f"[Drive] Upload run: {len(days)} day(s) to upload {[day_folder_name(d) for d in days]}")
            else:
                logger.info("[Drive] Upload run: nothing to upload")
            result = {"status": "ok", "days": {}, "pending_before": [day_folder_name(d) for d in days]}
            for day in days:
                try:
                    result["days"][day_folder_name(day)] = self.uploader.upload_day(day)
                except UploadCancelled:
                    result["status"] = "stopped"
                    return result
                except Exception as exc:
                    # Keep the local files; the next run (or restart) retries this day.
                    result["days"][day_folder_name(day)] = {"status": "failed", "error": repr(exc)[:500]}
                    result["status"] = "partial"
            result["local_removed"] = self.uploader.prune_local(self._config.local_keep_days, today)
            if days:
                try:
                    result["drive"] = self.uploader.enforce_drive_budget(
                        self._config.drive_budget_gb, self._config.drive_prune_after_days, today)
                except UploadCancelled:
                    result["status"] = "stopped"
                self._log_run_summary(result)
            return result
        finally:
            self._running.release()

    def _today_wait_reason(self, today, now: datetime) -> str:
        try:
            if not self._session_closed(today, now):
                return "session not closed yet (last hour's file not written)"
            name = day_folder_name(today)
            if self._config.candles_enabled and not (self._config.data_dir / name / CANDLES_MARKER_NAME).is_file():
                return "broker candles still downloading"
            if self.uploader.has_late_candles(today):
                return "late candles to add"
            return "pending"
        except Exception as exc:
            return f"unknown ({exc!r})"

    def _log_run_summary(self, result: dict) -> None:
        try:
            outcome = {day: info.get("status") for day, info in (result.get("days") or {}).items()}
            drive = result.get("drive") or {}
            level = logging.INFO if result.get("status") == "ok" else logging.WARNING
            logger.log(level, f"[Drive] Upload run finished ({result.get('status')}): {outcome}"
                              + (f", recorder data in Drive {drive.get('recorder_usage_after_gb', drive.get('recorder_usage_gb'))} GB"
                                 f" of {drive.get('budget_gb')} GB budget" if drive.get("recorder_usage_gb") is not None else "")
                              + (f", Drive budget pruned {drive.get('pruned')}" if drive.get("pruned") else "")
                              + (f", local days removed {result.get('local_removed')}" if result.get("local_removed") else ""))
        except Exception as exc:
            logger.warning(f"[Drive] Upload summary log failed: {exc!r}")

    def backlog(self) -> list[str]:
        """Finished days still waiting for Drive (alert when non-empty).
        Cached for STATUS_CACHE_SECS: it scans the data folder."""
        try:
            cached = self._backlog_cache
            if cached is not None and time.monotonic() - cached[0] < STATUS_CACHE_SECS:
                return list(cached[1])
            now = datetime.now(IST)
            include_today = now.date() if now.time() >= self._config.upload_at else None
            days = [day_folder_name(d) for d in self.uploader.pending_days(now.date(), include_today, ready=self._ready(now))]
            self._backlog_cache = (time.monotonic(), days)
            return days
        except Exception as exc:
            logger.warning(f"[Recorder] Backlog check failed: {exc!r}")
            return []

    def status(self) -> dict:
        try:
            return {"last_run": self.last_run, "next_run_at": self.next_run_at, "backlog": self.backlog()}
        except Exception as exc:
            return {"status_error": repr(exc)}

    def close(self) -> None:
        """Stops any upload in progress (it resumes on the next run). Never blocks."""
        try:
            self._stop.set()
        except Exception as exc:
            logger.warning(f"[Recorder] Upload stop failed: {exc!r}")
