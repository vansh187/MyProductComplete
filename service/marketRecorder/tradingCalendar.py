"""
Trading days, session windows and the on-disk / Drive naming:
  data/market/DD-MM-YYYY/<stream>/part-HHMM.parquet
part-HHMM is the IST start of the window; windows are aligned to clock
boundaries from the session start, and the last one ends at the session end
(09:00-10:00, ..., 15:00-15:30 with 60-minute windows).

Holidays are the union of two files: the hand-maintained base list
(appconfig/nse_holidays.json, tracked in git) and the list fetched from
NSE at runtime (data/market/.nse_holidays.json, git-ignored - see
nseHolidays.py). Special sessions come from the base list only.
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path

from utils.market_hours import IST

logger = logging.getLogger(__name__)

DAY_FOLDER_FORMAT = "%d-%m-%Y"
_NS = 1_000_000_000


def day_folder_name(day: date) -> str:
    return day.strftime(DAY_FOLDER_FORMAT)


def parse_day_folder(name: str) -> date | None:
    try:
        return datetime.strptime(name, DAY_FOLDER_FORMAT).date()
    except (TypeError, ValueError):
        return None


def to_ns(moment: datetime) -> int:
    return int(moment.timestamp()) * _NS + moment.microsecond * 1000


def ist_from_ns(ns: int) -> datetime:
    # Integer split, not ns / 1e9: a float can't hold epoch ns exactly, so
    # 1 ns before a window boundary would round onto the boundary.
    seconds, remainder = divmod(ns, _NS)
    return datetime.fromtimestamp(seconds, IST) + timedelta(microseconds=remainder // 1000)


@dataclass(frozen=True)
class Window:
    day: date
    start: datetime   # IST, inclusive
    end: datetime     # IST, exclusive
    start_ns: int = field(init=False, compare=False)
    end_ns: int = field(init=False, compare=False)

    def __post_init__(self):
        object.__setattr__(self, "start_ns", to_ns(self.start))
        object.__setattr__(self, "end_ns", to_ns(self.end))

    @property
    def label(self) -> str:
        return self.start.strftime("%H%M")

    @property
    def file_name(self) -> str:
        return f"part-{self.label}.parquet"


class TradingCalendar:

    def __init__(self, session_start: time, session_end: time, window_minutes: int,
                 holidays_file: Path | None = None, holidays_cache_file: Path | None = None):
        self._session_start = session_start
        self._session_end = session_end
        self._window = timedelta(minutes=window_minutes)
        self._holidays: set[str] = set()
        self._special_sessions: set[str] = set()
        self._windows_by_day: dict[date, list[Window]] = {}
        self._holidays_file = holidays_file
        self._holidays_cache_file = holidays_cache_file
        self.reload_holidays()

    def _read(self, path: Path | None, required: bool) -> dict:
        if path is None:
            return {}
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            if required:
                logger.warning(f"[Recorder] Holiday list {path} not found - weekends only")
            return {}
        except Exception as exc:
            logger.warning(f"[Recorder] Holiday list {path} not loaded ({exc!r})")
            return {}

    def reload_holidays(self) -> None:
        """Re-reads both holiday files. Never raises; on failure the
        previously loaded lists stay in use."""
        try:
            base = self._read(self._holidays_file, required=True)
            fetched = self._read(self._holidays_cache_file, required=False)
            self._holidays = {str(d) for d in base.get("holidays", [])} | {str(d) for d in fetched.get("holidays", [])}
            self._special_sessions = {str(d) for d in base.get("special_sessions", [])}
        except Exception as exc:
            logger.warning(f"[Recorder] Holiday reload failed ({exc!r}) - keeping the previous list")

    def is_trading_day(self, day: date) -> bool:
        try:
            iso = day.isoformat()
            if iso in self._special_sessions:
                return True
            return day.weekday() < 5 and iso not in self._holidays
        except Exception as exc:
            logger.warning(f"[Recorder] is_trading_day({day!r}) failed: {exc!r}")
            return False

    def session_bounds(self, day: date) -> tuple[datetime, datetime]:
        return (datetime.combine(day, self._session_start, IST), datetime.combine(day, self._session_end, IST))

    def in_session(self, now: datetime) -> bool:
        try:
            now = now.astimezone(IST)
            if not self.is_trading_day(now.date()):
                return False
            start, end = self.session_bounds(now.date())
            return start <= now < end
        except Exception as exc:
            logger.warning(f"[Recorder] in_session failed: {exc!r}")
            return False

    def windows(self, day: date) -> list[Window]:
        try:
            cached = self._windows_by_day.get(day)
            if cached is not None:
                return cached
            start, end = self.session_bounds(day)
            result = []
            cursor = start
            while cursor < end:
                result.append(Window(day, cursor, min(cursor + self._window, end)))
                cursor += self._window
            if len(self._windows_by_day) > 32:
                self._windows_by_day.clear()
            self._windows_by_day[day] = result
            return result
        except Exception as exc:
            logger.warning(f"[Recorder] windows({day!r}) failed: {exc!r}")
            return []

    def window_for_ns(self, ns: int) -> Window | None:
        try:
            for window in self.windows(ist_from_ns(ns).date()):
                if window.start_ns <= ns < window.end_ns:
                    return window
        except Exception as exc:
            logger.warning(f"[Recorder] window_for_ns({ns!r}) failed: {exc!r}")
        return None

    def window_by_label(self, day: date, label: str) -> Window | None:
        try:
            return next((w for w in self.windows(day) if w.label == label), None)
        except Exception:
            return None
