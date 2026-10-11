"""
Broker 1-minute candles for every instrument recorded that day, fetched after
the close (spec: `candles` stream):

  DD-MM-YYYY/candles/candles_1m.parquet   every TPSeries field kept
  DD-MM-YYYY/.candles_done.json           status + tick-vs-candle check

Which instruments: every token with a SUBSCRIBE event that day (read from the
day's `events` files, so it survives restarts), named from the instruments
snapshot. Calls are throttled (RECORDER_CANDLES_CALLS_PER_SEC) and run on a
background thread after the close, never on the broker executor.

The check: for every candle minute with trades (every minute for indices)
while the token was subscribed, there must be at least one recorded tick in
that minute (or the next, for frames received just after the boundary).
Missing minutes inside a known GAP (WebSocket down, restart) are expected -
the candle is then the record of that minute; any other missing minute is
reported as unexplained.

A day without the marker is retried by the startup backfill (for
RECORDER_CANDLES_BACKFILL_DAYS); the upload waits for the marker.
"""

import json
import logging
import os
import threading
import time
from datetime import date, datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from service.marketRecorder import contract
from service.marketRecorder.config import RECORDER_VERSION
from service.marketRecorder.fileNames import (
    CANDLES_ATTEMPTS_NAME, CANDLES_DIR, CANDLES_FILE, CANDLES_MARKER_NAME, INSTRUMENTS_DIR, INSTRUMENTS_FILE, JOURNAL_DIR,
)
from service.marketRecorder.streamWriter import write_parquet_atomic
from service.marketRecorder.tradingCalendar import TradingCalendar, day_folder_name
from utils.market_hours import IST_OFFSET
from utils.safe_numbers import safe_float, safe_int

logger = logging.getLogger(__name__)

MARKER_NAME = CANDLES_MARKER_NAME
SOURCE = "shoonya_tpseries_1m"
_MINUTE_NS = 60_000_000_000
_NS = 1_000_000_000
MAX_ATTEMPTS = 3


def _price(value) -> float | None:
    number = safe_float(value)
    return number if number is not None and number > 0 else None


class DayCandleFetcher:

    def __init__(self, config, shoonya_getter, calendar: TradingCalendar, stop_event: threading.Event | None = None):
        self._config = config
        self._data_dir = Path(config.data_dir)
        self._shoonya_getter = shoonya_getter or (lambda: None)
        self._calendar = calendar
        self._stop = stop_event or threading.Event()
        self._lock = threading.Lock()   # one day at a time
        self.retry_wait_secs = 2.0      # x attempt number, between retries of one instrument
        self.last_result: dict | None = None

    # -- paths / marker --------------------------------------------------------

    def day_dir(self, day: date) -> Path:
        return self._data_dir / day_folder_name(day)

    def marker_path(self, day: date) -> Path:
        return self.day_dir(day) / MARKER_NAME

    def has_marker(self, day: date) -> bool:
        try:
            return self.marker_path(day).is_file()
        except Exception:
            return False

    def read_marker(self, day: date) -> dict | None:
        try:
            return json.loads(self.marker_path(day).read_text(encoding="utf-8"))
        except Exception:
            return None

    def _write_marker(self, day: date, marker: dict) -> None:
        path = self.marker_path(day)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(marker, indent=1), encoding="utf-8")
        os.replace(tmp, path)

    # -- what was recorded -------------------------------------------------------

    def _read_events(self, day: date) -> list[dict]:
        rows = []
        try:
            for path in sorted((self.day_dir(day) / "events").glob("part-*.parquet")):
                rows += pq.read_table(path, columns=["ts_recv_ns", "event_type", "token", "from_ns", "to_ns", "detail"]).to_pylist()
        except Exception as exc:
            logger.warning(f"[Recorder] Reading {day_folder_name(day)} events failed: {exc!r}")
        rows.sort(key=lambda r: r["ts_recv_ns"])
        return rows

    def subscriptions(self, day: date) -> tuple[dict[str, list[tuple[int, int]]], list[tuple[int, int]]]:
        """({token key: [(from_ns, to_ns), ...]}, [gap (from_ns, to_ns), ...])."""
        intervals: dict[str, list[tuple[int, int]]] = {}
        gaps: list[tuple[int, int]] = []
        open_since: dict[str, int] = {}
        _, session_end = self._calendar.session_bounds(day)
        end_ns = int(session_end.timestamp()) * _NS
        for event in self._read_events(day):
            try:
                kind, ts, key = event["event_type"], event["ts_recv_ns"], event.get("token")
                if kind == "SUBSCRIBE" and key:
                    open_since.setdefault(key, ts)
                elif kind == "UNSUBSCRIBE" and key and key in open_since:
                    intervals.setdefault(key, []).append((open_since.pop(key), ts))
                elif kind == "RECORDER_STOP":
                    for open_key, since in open_since.items():
                        intervals.setdefault(open_key, []).append((since, ts))
                    open_since.clear()
                elif kind == "GAP" and event.get("from_ns") and event.get("to_ns"):
                    gaps.append((event["from_ns"], event["to_ns"]))
            except Exception:
                continue
        for key, since in open_since.items():
            intervals.setdefault(key, []).append((since, end_ns))
        return intervals, gaps

    def _names(self, day: date) -> dict[str, str]:
        try:
            path = self.day_dir(day) / INSTRUMENTS_DIR / INSTRUMENTS_FILE
            table = pq.read_table(path, columns=["exchange", "token", "tsym"])
            return {f"{r['exchange']}|{r['token']}": r["tsym"] for r in table.to_pylist()}
        except Exception:
            return {}

    # -- fetching ----------------------------------------------------------------

    def _parse(self, raw: list, exchange: str, token: str, tsym: str) -> list[dict]:
        rows = []
        for candle in raw or []:
            try:
                moment = datetime.strptime(candle["time"], "%d-%m-%Y %H:%M:%S").replace(tzinfo=IST_OFFSET)
                rows.append({
                    "minute_ns": int(moment.timestamp()) * _NS, "exchange": exchange, "token": token, "tsym": tsym,
                    "open": _price(candle.get("into")), "high": _price(candle.get("inth")),
                    "low": _price(candle.get("intl")), "close": _price(candle.get("intc")),
                    "vwap": _price(candle.get("intvwap")),
                    "volume": safe_int(candle.get("intv")), "cum_volume": safe_int(candle.get("v")),
                    "oi": safe_int(candle.get("oi")), "oi_change": safe_int(candle.get("intoi")),
                    "source": SOURCE,
                })
            except Exception:
                continue
        return rows

    def _fetch_one(self, shoonya, key: str, start_s: float, end_s: float) -> list | None:
        exchange, token = key.split("|", 1)
        for attempt in range(1, MAX_ATTEMPTS + 1):
            if self._stop.is_set():
                return None
            try:
                raw = shoonya.get_raw_time_price_series(exchange, token, start_s, end_s, "1")
            except Exception as exc:
                logger.warning(f"[Recorder] Candles for {key} failed: {exc!r}")
                raw = None
            if raw is not None:
                return raw
            if attempt < MAX_ATTEMPTS:
                self._stop.wait(self.retry_wait_secs * attempt)
        return None

    def fetch_day(self, day: date) -> dict:
        """Downloads, saves and checks one day's candles. Never raises."""
        if not self._lock.acquire(blocking=False):
            return {"day": day_folder_name(day), "status": "busy"}
        try:
            result = self._fetch_day(day)
        except Exception as exc:
            logger.error(f"[Recorder] Candle download for {day_folder_name(day)} failed: {exc!r}", exc_info=exc)
            result = {"day": day_folder_name(day), "status": "failed", "error": repr(exc)[:300]}
        finally:
            self._lock.release()
        self.last_result = result
        return result

    # -- attempts ------------------------------------------------------------------

    def _attempts_path(self, day: date) -> Path:
        return self.day_dir(day) / CANDLES_ATTEMPTS_NAME

    def _read_attempts(self, day: date) -> dict:
        try:
            data = json.loads(self._attempts_path(day).read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _write_json(self, path: Path, data: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        os.replace(tmp, path)

    def journal_open(self, day: date) -> bool:
        """The day still has journal segments: its events/ticks files are not
        all written yet (recovery or the window close hasn't run)."""
        try:
            folder = self._data_dir / JOURNAL_DIR / day_folder_name(day)
            return folder.is_dir() and any(folder.rglob("*.arrows"))
        except Exception:
            return True

    def _read_saved_candles(self, day: date) -> list[dict]:
        try:
            path = self.day_dir(day) / CANDLES_DIR / CANDLES_FILE
            return pq.read_table(path).to_pylist() if path.is_file() else []
        except Exception as exc:
            logger.warning(f"[Recorder] Saved candles of {day_folder_name(day)} unreadable ({exc!r}) - refetching all")
            return []

    def _save_candles(self, day: date, rows: list[dict]) -> int:
        table = pa.Table.from_pylist(rows, schema=contract.CANDLES)
        table = table.sort_by([("minute_ns", "ascending"), ("exchange", "ascending"), ("token", "ascending")])
        table = table.replace_schema_metadata({
            "schema_version": contract.SCHEMA_VERSION, "stream": "candles", "trading_date": day.isoformat(),
            "row_count": str(table.num_rows), "recorder_version": RECORDER_VERSION, "vm_hostname": self._config.hostname,
        })
        write_parquet_atomic(table, self.day_dir(day) / CANDLES_DIR / CANDLES_FILE)
        return table.num_rows

    def _fetch_day(self, day: date) -> dict:
        """The marker (.candles_done.json) means "candle download finished"
        and is written only when it really is: every instrument fetched, no
        instruments that day, or the attempt cap reached (status partial /
        failed, listing what's missing). Anything else leaves no marker, so
        the candle loop retries."""
        day_name = day_folder_name(day)
        if self.journal_open(day):
            # The day's events may not be merged into their files yet.
            return {"day": day_name, "status": "journal_open"}
        intervals, gaps = self.subscriptions(day)
        if not intervals:
            marker = {"day": day_name, "status": "no_instruments"}
            self._write_marker(day, marker)
            return marker

        shoonya = self._shoonya_getter()
        if shoonya is None or not getattr(shoonya, "is_connected", False) or \
                not callable(getattr(shoonya, "get_raw_time_price_series", None)):
            # No marker and no attempt counted: retried once the session is back.
            logger.warning(f"[Recorder] Candles for {day_name} postponed - broker session not connected")
            return {"day": day_name, "status": "broker_unavailable"}

        # A retry fetches only what failed before and keeps what was saved.
        attempts = self._read_attempts(day)
        saved = self._read_saved_candles(day) if attempts.get("failed") else []
        retry_only = set(attempts.get("failed") or []) if saved else set()
        keys = sorted(k for k in intervals if not retry_only or k in retry_only)
        rows = [r for r in saved if f"{r['exchange']}|{r['token']}" not in set(keys)]

        names = self._names(day)
        start, end = self._calendar.session_bounds(day)
        start_s, end_s = start.timestamp(), end.timestamp()
        pause = 1.0 / max(0.2, float(self._config.candles_calls_per_sec))
        failed = []
        began = time.monotonic()
        for key in keys:
            if self._stop.is_set():
                return {"day": day_name, "status": "stopped"}
            raw = self._fetch_one(shoonya, key, start_s, end_s)
            if raw is None:
                failed.append(key)
            else:
                exchange, token = key.split("|", 1)
                rows += self._parse(raw, exchange, token, names.get(key) or token)
            self._stop.wait(pause)

        if failed and len(failed) == len(keys) and not rows:
            # Nothing came back at all (e.g. the session token just expired):
            # don't write an empty file; count the attempt.
            return self._count_failed_attempt(day, attempts, failed, intervals, began, rows=0)

        candle_count = self._save_candles(day, rows)
        if failed:
            return self._count_failed_attempt(day, attempts, failed, intervals, began, rows=candle_count)

        marker = {
            "day": day_name, "status": "complete", "instruments": len(intervals), "failed": [],
            "candles": candle_count, "attempts": int(attempts.get("attempts", 0)) + 1,
            "secs": round(time.monotonic() - began, 1),
            "check": self.compare_with_ticks(day, rows, intervals, gaps),
        }
        self._write_marker(day, marker)
        self._clear_attempts(day)
        level = logging.INFO if not marker["check"].get("missing_unexplained") else logging.WARNING
        logger.log(level, f"[Recorder] Candles for {day_name}: {marker}")
        return marker

    def _count_failed_attempt(self, day: date, attempts: dict, failed: list[str], intervals: dict,
                              began: float, rows: int) -> dict:
        """Records a failed attempt; at the cap, writes the final marker
        (partial/failed) so the day can be uploaded with an honest status."""
        day_name = day_folder_name(day)
        count = int(attempts.get("attempts", 0)) + 1
        self._write_json(self._attempts_path(day), {
            "attempts": count, "failed": failed, "candles": rows,
            "last_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        })
        cap = max(1, int(getattr(self._config, "candles_max_attempts", 6)))
        if count < cap:
            logger.warning(f"[Recorder] Candles for {day_name}: {len(failed)} instruments failed "
                           f"(attempt {count}/{cap}) - retrying later")
            return {"day": day_name, "status": "retrying", "attempts": count, "failed": failed}
        marker = {
            "day": day_name, "status": "partial" if rows else "failed", "instruments": len(intervals),
            "failed": failed, "candles": rows, "attempts": count, "secs": round(time.monotonic() - began, 1),
            "check": self.compare_with_ticks(day, self._read_saved_candles(day), intervals, self.subscriptions(day)[1]),
        }
        self._write_marker(day, marker)
        logger.error(f"[Recorder] Candles for {day_name} gave up after {count} attempts: {len(failed)} instruments missing")
        return marker

    def _clear_attempts(self, day: date) -> None:
        try:
            self._attempts_path(day).unlink(missing_ok=True)
        except OSError:
            pass

    # -- tick vs candle check ------------------------------------------------------

    def _tick_minutes(self, day: date) -> set[tuple[str, int]]:
        """(token key, minute index) of every recorded tick of the day."""
        found: set[tuple[str, int]] = set()
        for path in sorted((self.day_dir(day) / "ticks").glob("part-*.parquet")):
            table = pq.read_table(path, columns=["exchange", "token", "ts_recv_ns"])
            if not table.num_rows:
                continue
            keys = pc.binary_join_element_wise(table.column("exchange"), table.column("token"), "|")
            minutes = pa.array(table.column("ts_recv_ns").to_numpy() // _MINUTE_NS)
            unique = pa.table({"key": keys, "minute": minutes}).group_by(["key", "minute"]).aggregate([])
            found.update(zip(unique.column("key").to_pylist(), unique.column("minute").to_pylist()))
        return found

    def compare_with_ticks(self, day: date, candles: list[dict], intervals: dict, gaps: list) -> dict:
        try:
            ticks = self._tick_minutes(day)
            index_keys = set(self._config.index_tokens)
            checked = matched = in_gaps = 0
            unexplained: dict[str, int] = {}
            for candle in candles:
                key = f"{candle['exchange']}|{candle['token']}"
                traded = (candle.get("volume") or 0) > 0
                if not traded and key not in index_keys:
                    continue   # nothing traded: no tick is expected
                start, end = candle["minute_ns"], candle["minute_ns"] + _MINUTE_NS
                if not any(s <= start and end <= e for s, e in intervals.get(key, [])):
                    continue   # not (fully) subscribed during this minute
                checked += 1
                minute = start // _MINUTE_NS
                if (key, minute) in ticks or (key, minute + 1) in ticks:
                    matched += 1
                elif any(s < end and start < e for s, e in gaps):
                    in_gaps += 1
                else:
                    unexplained[key] = unexplained.get(key, 0) + 1
            worst = sorted(unexplained.items(), key=lambda item: -item[1])[:20]
            return {"checked_minutes": checked, "matched": matched, "missing_in_known_gaps": in_gaps,
                    "missing_unexplained": sum(unexplained.values()), "unexplained_by_instrument": dict(worst)}
        except Exception as exc:
            logger.warning(f"[Recorder] Tick/candle check failed: {exc!r}")
            return {"error": repr(exc)[:300]}

    # -- backfill ------------------------------------------------------------------

    def days_missing_candles(self, candidate_days: list[date], today: date, session_over_today: bool) -> list[date]:
        result = []
        try:
            for day in candidate_days:
                if (today - day).days > self._config.candles_backfill_days:
                    continue
                if day == today and not session_over_today:
                    continue
                if day > today or self.has_marker(day):
                    continue
                if not (self.day_dir(day) / "events").is_dir():
                    continue
                if self.journal_open(day):
                    continue   # recovery / window close still pending: events not final yet
                result.append(day)
        except Exception as exc:
            logger.warning(f"[Recorder] Candle backfill scan failed: {exc!r}")
        return result
