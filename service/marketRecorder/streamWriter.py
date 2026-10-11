"""
Per-stream writer (spec section 10):

  rows -> in-memory buffer per window
  every flush (30 s): buffer -> one Arrow IPC segment, written to .tmp,
      fsync'd and renamed:  .journal/DD-MM-YYYY/<stream>/<HHMM>-<ns>-<seq>.arrows
  at window end: every segment of that window -> sorted ->
      DD-MM-YYYY/<stream>/part-HHMM.parquet (.tmp + fsync + os.replace,
      so a half-written .parquet never exists) -> segments deleted

A crash loses at most the unflushed buffer (<= one flush interval).
Segments left behind are merged into their window's file by the recorder's
recovery on the next start, together with whatever is recorded after the
restart. Segments are deleted only after their window's file is written, so
a failed write never loses data - it is retried later.

Runs only on the recorder's worker thread.
"""

import logging
import os
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from service.marketRecorder import contract
from service.marketRecorder.fileNames import JOURNAL_DIR  # noqa: F401  (re-exported)
from service.marketRecorder.tradingCalendar import TradingCalendar, Window, day_folder_name, parse_day_folder

logger = logging.getLogger(__name__)
_SEGMENT_SUFFIX = ".arrows"


def fsync_file(path: Path) -> None:
    # "r+b": Windows refuses fsync on a read-only handle.
    with open(path, "r+b") as handle:
        os.fsync(handle.fileno())


def fsync_dir(path: Path) -> None:
    """Makes a rename durable on POSIX; directories can't be opened on Windows.
    Best effort: never raises."""
    if os.name != "posix":
        return
    try:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError as exc:
        logger.debug(f"[Recorder] fsync of directory {path} failed: {exc}")


def write_parquet_atomic(table: pa.Table, final_path: Path) -> None:
    """Raises on failure (the caller decides what to keep); never leaves a
    partial .parquet behind."""
    tmp_path = final_path.with_name(final_path.name + ".tmp")
    try:
        final_path.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(table, tmp_path, compression="zstd", row_group_size=100_000)
        fsync_file(tmp_path)
        os.replace(tmp_path, final_path)
        fsync_dir(final_path.parent)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def read_segment(path: Path, schema: pa.Schema) -> pa.Table:
    """Every complete record batch in a segment; a damaged tail is skipped."""
    batches = []
    try:
        with pa.OSFile(str(path), "rb") as source:
            reader = pa.ipc.open_stream(source)
            while True:
                try:
                    batches.append(reader.read_next_batch())
                except StopIteration:
                    break
    except Exception as exc:
        logger.warning(f"[Recorder] Segment {path.name} is damaged after {len(batches)} batches: {exc}")
    try:
        return pa.Table.from_batches(batches, schema=schema) if batches else schema.empty_table()
    except Exception as exc:
        logger.error(f"[Recorder] Segment {path.name} has an unexpected schema, skipped: {exc}")
        return schema.empty_table()


class StreamWriter:

    def __init__(self, stream: str, data_dir: Path, calendar: TradingCalendar, base_metadata: dict[str, str]):
        self.stream = stream
        self._schema = contract.WINDOWED_SCHEMAS[stream]
        self._sort_key = contract.SORT_KEY[stream]
        self._data_dir = Path(data_dir)
        self._calendar = calendar
        self._base_metadata = base_metadata
        self._buffers: dict[Window, list[dict]] = {}
        self._closed: set[Window] = set()
        self._last_window: Window | None = None   # fast path: most rows land in the current window
        self._seq = 0
        self.rows_outside_session = 0
        self.late_rows = 0   # arrived after their window's file was written (dropped)
        self.last_closed: dict[str, str] = {}   # {"file": ..., "rows": ...} of the last window written

    # -- paths ---------------------------------------------------------------

    def final_path(self, window: Window) -> Path:
        return self._data_dir / day_folder_name(window.day) / self.stream / window.file_name

    def _journal_dir(self, window: Window) -> Path:
        return self._data_dir / JOURNAL_DIR / day_folder_name(window.day) / self.stream

    def _segments(self, window: Window) -> list[Path]:
        try:
            folder = self._journal_dir(window)
            if not folder.is_dir():
                return []
            return sorted(folder.glob(f"{window.label}-*{_SEGMENT_SUFFIX}"))
        except Exception as exc:
            logger.warning(f"[Recorder] Listing {self.stream} segments failed: {exc!r}")
            return []

    # -- writing -------------------------------------------------------------

    def add(self, row: dict) -> None:
        try:
            ns = row[self._sort_key]
            window = self._last_window
            if window is None or not window.start_ns <= ns < window.end_ns:
                window = self._calendar.window_for_ns(ns)
                self._last_window = window
            if window is None:
                self.rows_outside_session += 1
                return
            if window in self._closed:
                self.late_rows += 1
                return
            self._buffers.setdefault(window, []).append(row)
        except Exception as exc:
            logger.warning(f"[Recorder] {self.stream} row not buffered: {exc!r}")

    def buffered_rows(self) -> int:
        try:
            return sum(len(rows) for rows in self._buffers.values())
        except Exception:
            return 0

    def flush(self) -> None:
        """Writes every buffered window to a new journal segment. Never raises;
        a window whose write failed keeps its rows for the next flush."""
        for window in list(self._buffers):
            self._flush_window(window)

    def _flush_window(self, window: Window) -> bool:
        rows = self._buffers.pop(window, None)
        if not rows:
            return True
        tmp_path = None
        try:
            table = pa.Table.from_pylist(rows, schema=self._schema)
            folder = self._journal_dir(window)
            folder.mkdir(parents=True, exist_ok=True)
            self._seq += 1
            # Wall-clock ns in the name keeps segment order across restarts.
            name = f"{window.label}-{rows[-1][self._sort_key]:020d}-{self._seq:06d}{_SEGMENT_SUFFIX}"
            tmp_path = folder / (name + ".tmp")
            with pa.OSFile(str(tmp_path), "wb") as sink:
                with pa.ipc.new_stream(sink, self._schema) as writer:
                    writer.write_table(table)
            fsync_file(tmp_path)
            os.replace(tmp_path, folder / name)
            return True
        except Exception as exc:
            logger.error(f"[Recorder] Journal write of {len(rows)} {self.stream} rows failed ({exc!r}) - kept for retry")
            self._buffers.setdefault(window, [])[:0] = rows
            if tmp_path is not None:
                try:
                    tmp_path.unlink(missing_ok=True)
                except OSError:
                    pass
            return False

    def close_window(self, window: Window) -> Path | None:
        """Merges the window's segments (and any buffered rows) into its
        Parquet file. Always writes a file, empty if nothing was recorded,
        so a closed window is distinguishable from a missing one. Returns
        None (segments kept on disk for a retry) if the write failed."""
        try:
            if not self._flush_window(window):
                return None
            final_path = self.final_path(window)
            segments = self._segments(window)
            if final_path.exists() and segments:
                # Crash between writing the file and deleting its segments: the
                # file already holds them.
                logger.info(f"[Recorder] {self.stream} {window.label}: file exists, dropping {len(segments)} merged segments")
            elif not final_path.exists():
                tables = [read_segment(path, self._schema) for path in segments]
                table = pa.concat_tables(tables) if tables else self._schema.empty_table()
                table = table.sort_by(self._sort_key)
                metadata = {
                    **self._base_metadata,
                    "schema_version": contract.SCHEMA_VERSION,
                    "stream": self.stream,
                    "trading_date": window.day.isoformat(),
                    "window_start_ns": str(window.start_ns),
                    "window_end_ns": str(window.end_ns),
                    "row_count": str(table.num_rows),
                }
                write_parquet_atomic(table.replace_schema_metadata(metadata), final_path)
                self.last_closed = {"file": str(final_path), "rows": str(table.num_rows)}
                logger.info(f"[Recorder] Closed {self.stream} {day_folder_name(window.day)} {window.file_name} ({table.num_rows} rows)")
            for path in segments:
                try:
                    path.unlink(missing_ok=True)
                except OSError as exc:
                    logger.warning(f"[Recorder] Could not delete merged segment {path.name}: {exc}")
            self._closed.add(window)
            return final_path
        except Exception as exc:
            logger.error(f"[Recorder] Closing {self.stream} {window.file_name} failed ({exc!r}) - segments kept for retry")
            return None

    def is_closed(self, window: Window) -> bool:
        try:
            return window in self._closed or self.final_path(window).exists()
        except Exception:
            return False

    # -- crash recovery ------------------------------------------------------

    def pending_windows(self) -> list[Window]:
        """Windows that still have journal segments on disk."""
        found: set[Window] = set()
        try:
            root = self._data_dir / JOURNAL_DIR
            if not root.is_dir():
                return []
            for day_dir in root.iterdir():
                day = parse_day_folder(day_dir.name)
                stream_dir = day_dir / self.stream
                if day is None or not stream_dir.is_dir():
                    continue
                for segment in stream_dir.glob(f"*{_SEGMENT_SUFFIX}"):
                    window = self._calendar.window_by_label(day, segment.name.split("-", 1)[0])
                    if window is not None:
                        found.add(window)
        except Exception as exc:
            logger.warning(f"[Recorder] Scanning {self.stream} journal failed: {exc!r}")
        return sorted(found, key=lambda w: w.start)

    def last_journal_ns(self, day) -> int | None:
        """Newest row time journalled for a day (segment names carry it)."""
        newest = None
        try:
            stream_dir = self._data_dir / JOURNAL_DIR / day_folder_name(day) / self.stream
            for segment in stream_dir.glob(f"*{_SEGMENT_SUFFIX}") if stream_dir.is_dir() else []:
                try:
                    ns = int(segment.name.split("-")[1])
                except (IndexError, ValueError):
                    continue
                newest = ns if newest is None else max(newest, ns)
        except Exception as exc:
            logger.warning(f"[Recorder] Reading {self.stream} journal times failed: {exc!r}")
        return newest
