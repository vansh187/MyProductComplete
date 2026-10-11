"""
The market data recorder (spec Step 1). A passenger on the product's
Shoonya WebSocket - it must never slow down or break the product:

  - tap() runs on the broker's WS thread and only stamps time.time_ns() and
    appends the raw frame to a bounded deque (O(1)). When the deque is full
    the frame is dropped and counted (QUEUE_OVERFLOW event), never blocking.
  - Everything else - forward-filling, journalling, Parquet files, option
    chain snapshots, subscription changes - runs on the recorder's own
    worker thread. Every step is wrapped; an error becomes an ERROR event
    and a log line. ensure_running() (watchdog) restarts a dead thread.
  - Subscriptions go through the shared feed's ref-counted
    ensure_subscribed/release, so the product's own subscriptions are never
    touched and no second broker login is made. A token the product already
    holds is re-subscribed so the broker sends the recorder a full first
    frame (prev close, open, OI...) instead of only later partial updates.

Files: data/market/DD-MM-YYYY/<stream>/part-HHMM.parquet (one per window),
plus DD-MM-YYYY/instruments/snapshot.parquet written at session start.
"""

import collections
import json
import logging
import shutil
import statistics
import threading
import time
from datetime import datetime

import pyarrow as pa
import pyarrow.parquet as pq

from marketengine.feedTypes import DEPTH_FEED
from service.marketRecorder import contract
from service.marketRecorder.candleFetcher import DayCandleFetcher
from service.marketRecorder.config import RECORDER_VERSION, RecorderConfig, load_recorder_config
from service.marketRecorder.fileNames import INSTRUMENTS_DIR, INSTRUMENTS_FILE
from service.marketRecorder.nseHolidays import refresh_holiday_file
from service.marketRecorder.rowBuilder import RowBuilder
from service.marketRecorder.streamWriter import StreamWriter, write_parquet_atomic
from service.marketRecorder.subscriptionPlanner import SubscriptionPlanner
from service.marketRecorder.tradingCalendar import TradingCalendar, day_folder_name, ist_from_ns, parse_day_folder
from utils.market_hours import IST

logger = logging.getLogger(__name__)

HOUSEKEEPING_SECS = 1.0
# A window's file is written this long after it ends, so frames stamped just
# before the boundary but still in the queue make it into the file.
WINDOW_CLOSE_GRACE_NS = 2_000_000_000
IDLE_SLEEP_SECS = 0.02
MAX_FRAMES_PER_PASS = 5_000
CLOCK_CHECK_SECS = 60.0
CLOCK_WARNING_COOLDOWN_SECS = 600.0
# ts_recv - ft is normally in [0, ~1.5 s): 'ft' has 1-second resolution.
CLOCK_OFFSET_LIMIT_SECS = (-1.5, 3.0)
# A failed session start is retried this often, not every second.
SESSION_RETRY_SECS = 60.0
# Without option chains (master not loaded yet), the day plan is reloaded this often.
MASTER_RETRY_SECS = 300.0


class MarketRecorder:

    def __init__(self, feed, config: RecorderConfig | None = None, planner: SubscriptionPlanner | None = None,
                 clock_ns=time.time_ns, shoonya_getter=None):
        """shoonya_getter() -> the current ShoonyaConnection (or None), for
        the after-close candle download; a getter because a re-login
        replaces the connection object."""
        self._feed = feed
        self._config = config or load_recorder_config()
        self._clock_ns = clock_ns
        self.calendar = TradingCalendar(self._config.session_start, self._config.session_end,
                                        self._config.window_minutes, self._config.holidays_file,
                                        self._config.holidays_cache_file)
        self.planner = planner or SubscriptionPlanner(self._config)
        self._rows = RowBuilder(set(self._config.index_tokens), self.planner.tsym)
        self._base_metadata = {
            "window_minutes": str(self._config.window_minutes),
            "recorder_version": RECORDER_VERSION,
            "vm_hostname": self._config.hostname,
        }
        self.writers = {stream: StreamWriter(stream, self._config.data_dir, self.calendar, self._base_metadata)
                        for stream in contract.WINDOWED_SCHEMAS}

        self._queue: collections.deque = collections.deque()
        self._queue_max = self._config.queue_max
        self._accepting = False          # read by tap() on the WS thread
        self._dropped = 0
        self._dropped_from_ns = None
        self._dropped_to_ns = None
        self._dropped_reported = 0

        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._active_day = None
        self._planned_day = None
        self._open_windows: set = set()
        self._touchline: set[str] = set()
        self._depth: set[str] = set()
        self._ws_connected: bool | None = None
        self._ws_down_since_ns: int | None = None
        self._clock_offsets: collections.deque = collections.deque(maxlen=500)
        self._last_clock_warning = 0.0
        self._session_retry_at = 0.0
        self._next_master_retry = 0.0
        self._next = {"housekeeping": 0.0, "flush": 0.0, "chain": 0.0, "plan": 0.0, "clock": 0.0}
        self._frames = 0
        self._rows_today = {stream: 0 for stream in contract.WINDOWED_SCHEMAS}
        self.thread_restarts = 0
        self.last_error: str | None = None
        self.errors_total = 0
        self.candles = DayCandleFetcher(self._config, shoonya_getter, self.calendar, stop_event=self._stop)
        self._recovered = threading.Event()    # journal recovery done: day files are final
        self._candle_wake = threading.Event()  # session end: fetch now instead of at the next check
        self._candle_thread: threading.Thread | None = None

        try:
            if hasattr(feed, "on_raw_tick"):
                feed.on_raw_tick(self.tap)
        except Exception as exc:
            logger.error(f"[Recorder] Could not attach to the market feed: {exc!r}")

    # -- WS thread -------------------------------------------------------------

    def tap(self, raw: dict) -> None:
        """Called on the broker's WS thread for every frame. O(1); never raises."""
        try:
            if not self._accepting:
                return
            now_ns = time.time_ns()
            if len(self._queue) >= self._queue_max:
                self._dropped += 1
                if self._dropped_from_ns is None:
                    self._dropped_from_ns = now_ns
                self._dropped_to_ns = now_ns
                return
            self._queue.append((now_ns, raw))
        except Exception:
            pass

    # -- lifecycle ----------------------------------------------------------------

    def start(self) -> None:
        try:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="market-recorder", daemon=True)
            self._thread.start()
            logger.info(f"[Recorder] Started ({RECORDER_VERSION}), data dir {self._config.data_dir}")
            self.refresh_holidays_in_background()
            self._start_candle_loop()
        except Exception as exc:
            logger.error(f"[Recorder] Start failed: {exc!r}")

    def _start_candle_loop(self) -> None:
        try:
            if not self._config.candles_enabled:
                return
            if self._candle_thread is not None and self._candle_thread.is_alive():
                return
            self._candle_thread = threading.Thread(target=self._candle_loop, name="recorder-candles", daemon=True)
            self._candle_thread.start()
        except Exception as exc:
            logger.error(f"[Recorder] Could not start the candle loop: {exc!r}")

    def fetch_candles_in_background(self, day=None) -> None:
        """Asks the candle loop to run now (e.g. right after the session end)."""
        try:
            self._start_candle_loop()
            self._candle_wake.set()
        except Exception as exc:
            logger.error(f"[Recorder] Could not wake the candle loop: {exc!r}")

    def _candle_loop(self) -> None:
        """One background thread for every broker-candle download: waits for
        the journal recovery (so a crashed day's events are final), then
        every candles_retry_secs - or at once when the session ends - fetches
        each recent day still without its final marker. A busy broker, a
        dropped session or a failed instrument is simply retried on the next
        pass; nothing is fetched twice at the same time."""
        try:
            while not self._recovered.wait(1.0):
                if self._stop.is_set():
                    return
            while not self._stop.is_set():
                try:
                    for day in self._days_missing_candles():
                        if self._stop.is_set():
                            return
                        self.candles.fetch_day(day)
                except Exception as exc:
                    logger.error(f"[Recorder] Candle pass failed: {exc!r}")
                self._candle_wake.wait(self._config.candles_retry_secs)
                self._candle_wake.clear()
        except Exception as exc:
            logger.error(f"[Recorder] Candle loop stopped: {exc!r}")

    def _days_missing_candles(self) -> list:
        """Recent recorded days (still on disk) whose candle download hasn't
        finished - uploaded or not: late candles are uploaded too."""
        try:
            now = ist_from_ns(self._clock_ns())
            days = []
            if self._config.data_dir.is_dir():
                for folder in self._config.data_dir.iterdir():
                    parsed = parse_day_folder(folder.name)
                    if parsed is not None and folder.is_dir():
                        days.append(parsed)
            _, session_end = self.calendar.session_bounds(now.date())
            session_over = now >= session_end and self._active_day != now.date()
            return self.candles.days_missing_candles(sorted(days), now.date(), session_over)
        except Exception as exc:
            logger.warning(f"[Recorder] Candle backfill scan failed: {exc!r}")
            return []

    def refresh_holidays_in_background(self) -> None:
        """Refreshes the NSE holiday cache on its own thread (network call;
        never on the recorder or WS thread), then reloads the calendar."""
        try:
            if not self._config.refresh_holidays:
                return

            def _refresh():
                try:
                    if refresh_holiday_file(self._config.holidays_cache_file):
                        self.calendar.reload_holidays()
                except Exception as exc:
                    logger.warning(f"[Recorder] Holiday refresh failed: {exc!r}")

            threading.Thread(target=_refresh, name="recorder-holidays", daemon=True).start()
        except Exception as exc:
            logger.warning(f"[Recorder] Could not start the holiday refresh: {exc!r}")

    def ensure_running(self) -> bool:
        """Watchdog: restarts the worker thread if it died. True if it was restarted."""
        try:
            if not self._stop.is_set():
                self._start_candle_loop()   # restarts the candle loop too if it died
            if self._stop.is_set() or (self._thread is not None and self._thread.is_alive()):
                return False
            self.thread_restarts += 1
            logger.error(f"[Recorder] Worker thread was dead - restarting (restart #{self.thread_restarts})")
            self.start()
            return True
        except Exception as exc:
            logger.error(f"[Recorder] Watchdog restart failed: {exc!r}")
            return False

    def stop(self, timeout: float = 10.0) -> None:
        """Shutdown: journals everything buffered. The open window is NOT
        closed - after a restart within the window it is merged into one file."""
        try:
            self._stop.set()
            self._candle_wake.set()   # end the candle loop's wait at once
            if self._thread is not None:
                self._thread.join(timeout=timeout)
        except Exception as exc:
            logger.error(f"[Recorder] Stop failed: {exc!r}")

    # -- worker thread --------------------------------------------------------------

    def _run(self) -> None:
        self._guard("recovery", self._recover)
        self._recovered.set()
        while not self._stop.is_set():
            try:
                processed = self._drain()
                now = time.monotonic()
                if now >= self._next["housekeeping"]:
                    self._next["housekeeping"] = now + HOUSEKEEPING_SECS
                    self._housekeeping(now)
                if not processed:
                    time.sleep(IDLE_SLEEP_SECS)
            except Exception as exc:
                self._report_error("loop", exc)
                time.sleep(1.0)
        self._guard("shutdown", self._shutdown)

    def _guard(self, label: str, func, *args) -> bool:
        """Runs func; an exception is reported, never propagated. True on success."""
        try:
            func(*args)
            return True
        except Exception as exc:
            self._report_error(label, exc)
            return False

    def _report_error(self, label: str, exc: Exception) -> None:
        """Logs the error and records an ERROR event. Outside the session no
        window exists for the event; it is then only logged and counted."""
        try:
            self.last_error = f"{label}: {exc!r}"
            self.errors_total += 1
            logger.error(f"[Recorder] {label} failed: {exc!r}", exc_info=exc)
            self._event("ERROR", detail=json.dumps({"where": label, "error": repr(exc)[:500]}))
        except Exception:
            pass

    def _drain(self, limit: int = MAX_FRAMES_PER_PASS) -> int:
        processed = 0
        try:
            queue = self._queue
            while queue and processed < limit:
                recv_ns, raw = queue.popleft()
                processed += 1
                try:
                    self._ingest(recv_ns, raw)
                except Exception as exc:
                    self._report_error("ingest", exc)
        except Exception as exc:
            self._report_error("drain", exc)
        self._frames += processed
        return processed

    def _drain_backlog(self) -> None:
        """Processes the frames queued up to now - bounded by the queue length
        at the start, so a burst arriving meanwhile can't keep the worker
        here (it is handled by the normal loop)."""
        try:
            remaining = len(self._queue)
            while remaining > 0:
                processed = self._drain(min(remaining, MAX_FRAMES_PER_PASS))
                if not processed:
                    break
                remaining -= processed
        except Exception as exc:
            self._report_error("drain backlog", exc)

    def _ingest(self, recv_ns: int, raw: dict) -> None:
        try:
            frame_type = raw.get("t")
            if frame_type in ("tk", "tf"):
                key = f"{raw.get('e')}|{raw.get('tk')}"
                if key not in self._touchline:
                    return   # a product subscription, not ours
                row = self._rows.tick_row(recv_ns, raw)
                if row is not None:
                    self._add("ticks", row)
                    exch_ns = row["ts_exch_ns"]
                    if exch_ns:
                        self._clock_offsets.append((recv_ns - exch_ns) / 1e9)
            elif frame_type in ("dk", "df"):
                key = f"{raw.get('e')}|{raw.get('tk')}"
                if key not in self._depth:
                    return
                row = self._rows.depth_row(recv_ns, raw)
                if row is not None:
                    self._add("depth", row)
        except Exception as exc:
            self._report_error("ingest", exc)

    def _add(self, stream: str, row: dict) -> None:
        try:
            self.writers[stream].add(row)
            self._rows_today[stream] += 1
        except Exception as exc:
            logger.warning(f"[Recorder] {stream} row not added: {exc!r}")

    def _event(self, event_type: str, stream=None, token=None, from_ns=None, to_ns=None, detail=None, ts_ns=None) -> None:
        try:
            self._add("events", {
                "ts_recv_ns": ts_ns if ts_ns is not None else self._clock_ns(), "event_type": event_type,
                "stream": stream, "token": token, "from_ns": from_ns, "to_ns": to_ns, "detail": detail,
            })
        except Exception as exc:
            logger.warning(f"[Recorder] {event_type} event not recorded: {exc!r}")

    # -- housekeeping (once a second) ---------------------------------------------

    def _housekeeping(self, now_mono: float) -> None:
        now_ns = self._clock_ns()
        now = ist_from_ns(now_ns)
        in_session = self.calendar.in_session(now)

        if in_session and self._active_day != now.date() and now_mono >= self._session_retry_at:
            if not self._guard("session start", self._begin_session, now_ns):
                # Back off: one clear error a minute, not one every second.
                self._session_retry_at = now_mono + SESSION_RETRY_SECS
        if self._active_day is not None:
            self._guard("ws state", self._check_ws, now_ns)
            self._guard("drops", self._report_drops)
            if now_mono >= self._next_master_retry and not self.planner.option_keys():
                self._next_master_retry = now_mono + MASTER_RETRY_SECS
                self._guard("day plan reload", self._reload_day_plan, now_ns)
            if now_mono >= self._next["plan"]:
                self._next["plan"] = now_mono + self._config.replan_secs
                self._guard("subscriptions", self._apply_plan, now_ns)
            if now_mono >= self._next["chain"]:
                self._next["chain"] = now_mono + self._config.chain_snapshot_secs
                self._guard("chain snapshot", self._chain_snapshot, now_ns)
            if now_mono >= self._next["clock"]:
                self._next["clock"] = now_mono + CLOCK_CHECK_SECS
                self._guard("clock check", self._check_clock, now_ns)
            if not in_session:
                self._guard("session end", self._end_session, now_ns)

        self._guard("window close", self._close_finished_windows, now_ns)
        if now_mono >= self._next["flush"]:
            self._next["flush"] = now_mono + self._config.journal_flush_secs
            for writer in self.writers.values():
                self._guard(f"flush {writer.stream}", writer.flush)

    def _begin_session(self, now_ns: int) -> None:
        """Raises on failure - the caller backs off and retries."""
        day = ist_from_ns(now_ns).date()
        if self._planned_day != day:
            self._rows.reset_day()
            self.planner.start_day(ist_from_ns(now_ns))
            self._planned_day = day
            self._rows_today = {stream: 0 for stream in contract.WINDOWED_SCHEMAS}
            self._next_master_retry = time.monotonic() + MASTER_RETRY_SECS
        self._active_day = day
        self._accepting = True
        self._ws_connected = None

        last_ns = self._last_recorded_ns(day)
        self._event("RECORDER_START", detail=json.dumps({
            "version": RECORDER_VERSION, "host": self._config.hostname, "window_minutes": self._config.window_minutes,
        }))
        if self.planner.load_errors:
            self._event("ERROR", detail=json.dumps({"where": "day plan", "errors": self.planner.load_errors[:20]}))
        if last_ns is not None and last_ns < now_ns:
            # Restarted mid-day: everything between the last saved row and now is missing.
            self._event("GAP", from_ns=last_ns, to_ns=now_ns, detail="recorder restart")
        self._guard("instruments snapshot", self._write_instruments, day)
        self._guard("subscriptions", self._apply_plan, now_ns)
        logger.info(f"[Recorder] Session started for {day_folder_name(day)}")

    def _reload_day_plan(self, now_ns: int) -> None:
        """No option chains yet (scrip master missing or stale at the open):
        reload the plan from the masters, which refresh themselves."""
        self.planner.start_day(ist_from_ns(now_ns))
        if self.planner.option_keys():
            logger.info("[Recorder] Option chains available after reloading the day plan")
            self._write_instruments(self._active_day, overwrite=True)
            self._apply_plan(now_ns)

    def _end_session(self, now_ns: int) -> None:
        day = self._active_day
        windows = self.calendar.windows(day)
        last_window = windows[-1] if windows else None
        # Session-end events are stamped inside the last window (1 ns before
        # the close), otherwise they would fall outside every window.
        stop_ns = min(now_ns, last_window.end_ns - 1) if last_window else now_ns
        self._accepting = False
        self._drain_backlog()
        self._event("RECORDER_STOP", detail="session end", ts_ns=stop_ns)
        self._guard("release subscriptions", self._release_all, stop_ns)
        for writer in self.writers.values():
            try:
                for window in writer.pending_windows():
                    if window.day == day and not writer.is_closed(window):
                        writer.close_window(window)
                if last_window is not None and not writer.is_closed(last_window):
                    writer.close_window(last_window)
            except Exception as exc:
                self._report_error(f"closing {writer.stream} at session end", exc)
        self._active_day = None
        logger.info(f"[Recorder] Session ended for {day_folder_name(day)}: rows {self._rows_today}")
        self.refresh_holidays_in_background()
        self.fetch_candles_in_background(day)

    def _shutdown(self) -> None:
        now_ns = self._clock_ns()
        if self._active_day is not None:
            self._event("RECORDER_STOP", detail="process shutdown", ts_ns=now_ns)
        self._accepting = False
        self._drain_backlog()
        self._guard("release subscriptions", self._release_all, now_ns)
        for writer in self.writers.values():
            self._guard(f"flush {writer.stream}", writer.flush)

    # -- windows -------------------------------------------------------------------

    def _recover(self) -> None:
        """Startup: windows left with journal segments that have already
        ended are written now; the window in progress keeps its segments and
        is merged when it ends."""
        now_ns = self._clock_ns()
        for writer in self.writers.values():
            try:
                for window in writer.pending_windows():
                    if window.end_ns + WINDOW_CLOSE_GRACE_NS <= now_ns:
                        writer.close_window(window)
                    else:
                        self._open_windows.add(window)
            except Exception as exc:
                self._report_error(f"recovering {writer.stream}", exc)

    def _close_finished_windows(self, now_ns: int) -> None:
        current = self.calendar.window_for_ns(now_ns)
        if current is not None and self._active_day is not None:
            self._open_windows.add(current)
        for window in sorted(self._open_windows, key=lambda w: w.start):
            if window.end_ns + WINDOW_CLOSE_GRACE_NS > now_ns:
                continue
            self._drain_backlog()
            if self._queue and self._queue[0][0] < window.end_ns:
                # Frames of this window are still queued: close on a later pass,
                # so no frame ever arrives after its window's file is written.
                continue
            for writer in self.writers.values():
                try:
                    if not writer.is_closed(window):
                        writer.close_window(window)   # on failure segments stay; retried at session end / restart
                except Exception as exc:
                    self._report_error(f"closing {writer.stream} {window.label}", exc)
            self._open_windows.discard(window)

    def _last_recorded_ns(self, day) -> int | None:
        """Newest row time already saved today (journal or closed files)."""
        newest = None
        try:
            for writer in self.writers.values():
                ns = writer.last_journal_ns(day)
                if ns is not None:
                    newest = ns if newest is None else max(newest, ns)
            if newest is None:
                ticks_dir = self._config.data_dir / day_folder_name(day) / "ticks"
                for path in sorted(ticks_dir.glob("part-*.parquet"), reverse=True) if ticks_dir.is_dir() else []:
                    column = pq.read_table(path, columns=["ts_recv_ns"]).column("ts_recv_ns")
                    if len(column):
                        newest = max(column.to_pylist())
                        break
        except Exception as exc:
            logger.warning(f"[Recorder] Could not read the last recorded time: {exc!r}")
        return newest

    # -- subscriptions -----------------------------------------------------------

    def _spot(self, underlying: str) -> float | None:
        try:
            index_key = self.planner.index_key_by_underlying.get(underlying)
            spot = self._rows.ltp(index_key) if index_key else None
            if spot is None and underlying in self.planner.front_futures:
                spot = self._rows.ltp(self.planner.front_futures[underlying])
            return spot
        except Exception:
            return None

    def _apply_plan(self, now_ns: int) -> None:
        touchline, depth = self.planner.plan(self._spot)
        self._swap(touchline, depth, now_ns)

    def _held_by_others(self, keys: set[str], count_method: str) -> set[str]:
        """Keys someone else (the product) already subscribes to on the shared feed."""
        try:
            count = getattr(self._feed, count_method, None)
            if not callable(count):
                return set()
            return {key for key in keys if count(key) > 0}
        except Exception as exc:
            logger.warning(f"[Recorder] Could not read feed subscription counts: {exc!r}")
            return set()

    def _swap(self, touchline: set[str], depth: set[str], now_ns: int) -> None:
        """Subscribe new tokens BEFORE releasing old ones (no hole in coverage)."""
        add_t, drop_t = touchline - self._touchline, self._touchline - touchline
        add_d, drop_d = depth - self._depth, self._depth - depth
        # Tokens the product already holds get no new subscribe frame from the
        # ref-counted feed, so the recorder would never see their full first
        # frame - ask the broker to resend it.
        held_t = self._held_by_others(add_t, "subscribed_count")
        held_d = self._held_by_others(add_d, "depth_subscribed_count")
        try:
            if add_t:
                self._feed.ensure_subscribed(add_t)
            if add_d:
                self._feed.ensure_depth_subscribed(add_d)
            resubscribe = getattr(self._feed, "resubscribe", None)
            if callable(resubscribe):
                if held_t:
                    resubscribe(held_t)
                if held_d:
                    resubscribe(held_d, feed_type=DEPTH_FEED)
        finally:
            # The feed never raises, but keep the recorder's view consistent regardless.
            self._touchline = set(touchline)
            self._depth = set(depth)
        if drop_t:
            self._feed.release(drop_t)
        if drop_d:
            self._feed.release_depth(drop_d)
        for stream, keys, event in (("ticks", add_t, "SUBSCRIBE"), ("depth", add_d, "SUBSCRIBE"),
                                    ("ticks", drop_t, "UNSUBSCRIBE"), ("depth", drop_d, "UNSUBSCRIBE")):
            for key in sorted(keys):
                self._event(event, stream=stream, token=key, ts_ns=now_ns, detail=self.planner.tsym(key))
        if add_t or drop_t or add_d or drop_d:
            logger.info(f"[Recorder] Subscriptions: +{len(add_t)}/-{len(drop_t)} touchline, "
                        f"+{len(add_d)}/-{len(drop_d)} depth (now {len(self._touchline)} + {len(self._depth)}), "
                        f"re-sent {len(held_t) + len(held_d)} already held by the product")

    def _release_all(self, ts_ns: int) -> None:
        if self._touchline or self._depth:
            self._swap(set(), set(), ts_ns)

    # -- periodic checks -----------------------------------------------------------

    def _check_ws(self, now_ns: int) -> None:
        connected = bool(getattr(self._feed, "is_connected", False))
        if connected == self._ws_connected:
            return
        if not connected:
            self._ws_down_since_ns = now_ns
            self._event("WS_DISCONNECT", from_ns=now_ns, ts_ns=now_ns)
        elif self._ws_connected is False:
            down_since = self._ws_down_since_ns or now_ns
            self._event("WS_RECONNECT", ts_ns=now_ns)
            self._event("GAP", from_ns=down_since, to_ns=now_ns, detail="websocket down", ts_ns=now_ns)
        self._ws_connected = connected

    def _report_drops(self) -> None:
        dropped = self._dropped
        if dropped > self._dropped_reported:
            self._event("QUEUE_OVERFLOW", from_ns=self._dropped_from_ns, to_ns=self._dropped_to_ns,
                        detail=json.dumps({"dropped": dropped - self._dropped_reported, "dropped_total": dropped}))
            logger.warning(f"[Recorder] Queue full: dropped {dropped - self._dropped_reported} frames")
            self._dropped_reported = dropped
            self._dropped_from_ns = None

    def _check_clock(self, now_ns: int) -> None:
        if len(self._clock_offsets) < 50:
            return
        offset = statistics.median(self._clock_offsets)
        low, high = CLOCK_OFFSET_LIMIT_SECS
        if (offset < low or offset > high) and time.monotonic() - self._last_clock_warning > CLOCK_WARNING_COOLDOWN_SECS:
            self._last_clock_warning = time.monotonic()
            self._event("CLOCK_WARNING", ts_ns=now_ns, detail=json.dumps({"median_recv_minus_exch_secs": round(offset, 3)}))
            logger.warning(f"[Recorder] Clock check: receive time minus exchange time is {offset:.2f}s - check NTP/chrony")

    # -- chain snapshots / instruments -------------------------------------------

    def _chain_snapshot(self, snapshot_ns: int) -> None:
        spots, futures = {}, {}
        for key in list(self._touchline):
            try:
                info = self.planner.instruments.get(key)
                if info is None or info["instrument_type"] != "OPT":
                    continue
                latest = self._rows.latest(key)
                if latest is None:
                    continue
                fields, recv_ns = latest
                underlying = info["underlying"]
                if underlying not in spots:
                    spots[underlying] = self._rows.ltp(self.planner.index_key_by_underlying.get(underlying, ""))
                    front = self.planner.front_futures.get(underlying)
                    futures[underlying] = self._rows.ltp(front) if front else None
                self._add("chain", {
                    "snapshot_ns": snapshot_ns, "ts_recv_ns": recv_ns, "underlying": underlying,
                    "spot": spots[underlying], "future": futures[underlying],
                    "exchange": info["exchange"], "token": info["token"], "tsym": info["tsym"] or info["token"],
                    "expiry": info["expiry"], "strike": info["strike"], "opt_type": info["opt_type"],
                    "ltp": fields.get("ltp"), "bid": fields.get("bid"), "ask": fields.get("ask"),
                    "bid_qty": fields.get("bid_qty"), "ask_qty": fields.get("ask_qty"),
                    "volume": fields.get("volume"), "oi": fields.get("oi"),
                    "iv": None, "delta": None, "gamma": None, "theta": None, "vega": None, "greeks_source": None,
                })
            except Exception as exc:
                logger.warning(f"[Recorder] Chain row for {key} skipped: {exc!r}")

    def _write_instruments(self, day, overwrite: bool = False) -> None:
        path = self._config.data_dir / day_folder_name(day) / INSTRUMENTS_DIR / INSTRUMENTS_FILE
        if path.exists() and not overwrite:
            return
        rows = []
        for info in self.planner.instruments.values():
            try:
                rows.append({
                    "exchange": info["exchange"], "token": info["token"], "tsym": info["tsym"] or info["token"],
                    "underlying": info["underlying"], "instrument_type": info["instrument_type"], "expiry": info["expiry"],
                    "strike": info["strike"], "opt_type": info["opt_type"],
                    "lot_size": int(info["lot_size"]) if info.get("lot_size") else None,
                    "tick_size": info.get("tick_size"), "freeze_qty": None,
                })
            except Exception as exc:
                logger.warning(f"[Recorder] Instrument {info.get('token')} left out of the snapshot: {exc!r}")
        table = pa.Table.from_pylist(rows, schema=contract.INSTRUMENTS)
        table = table.replace_schema_metadata({
            "schema_version": contract.SCHEMA_VERSION, "stream": "instruments", "trading_date": day.isoformat(),
            "row_count": str(table.num_rows), "recorder_version": RECORDER_VERSION, "vm_hostname": self._config.hostname,
        })
        write_parquet_atomic(table, path)
        logger.info(f"[Recorder] Instruments snapshot written ({table.num_rows} contracts)")

    # -- monitoring --------------------------------------------------------------

    def status(self) -> dict:
        """Snapshot for /api/internal/latency. Never raises."""
        try:
            disk = None
            try:
                target = self._config.data_dir if self._config.data_dir.exists() else self._config.data_dir.parent
                usage = shutil.disk_usage(target)
                disk = {"free_gb": round(usage.free / 1e9, 1), "free_pct": round(usage.free / usage.total * 100, 1)}
            except OSError:
                pass
            return {
                "enabled": True,
                "thread_alive": bool(self._thread and self._thread.is_alive()),
                "thread_restarts": self.thread_restarts,
                "recording_day": day_folder_name(self._active_day) if self._active_day else None,
                "in_session": self.calendar.in_session(datetime.now(IST)),
                "queue_depth": len(self._queue),
                "dropped_frames": self._dropped,
                "frames_processed": self._frames,
                "rows_today": dict(self._rows_today),
                "subscribed": {"touchline": len(self._touchline), "depth": len(self._depth)},
                "ws_connected": self._ws_connected,
                "last_file_closed": {stream: writer.last_closed for stream, writer in self.writers.items()},
                "late_rows_dropped": {stream: writer.late_rows for stream, writer in self.writers.items()},
                "rows_outside_session": {stream: writer.rows_outside_session for stream, writer in self.writers.items()},
                "plan_load_errors": list(self.planner.load_errors),
                "candles_last_result": self.candles.last_result,
                "errors_total": self.errors_total,
                "last_error": self.last_error,
                "disk": disk,
            }
        except Exception as exc:
            return {"enabled": True, "status_error": repr(exc)}
