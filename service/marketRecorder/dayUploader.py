"""
End-of-day upload of one trading day to Google Drive (spec sections 11-12):

  data/market/DD-MM-YYYY/<stream>/*.parquet  ->  <root>/DD-MM-YYYY/<stream>/*.parquet
  then <root>/DD-MM-YYYY/_manifest.json, uploaded LAST: its presence in
  Drive means the whole day is there (the engine only syncs such days).

Every file is verified by md5 against Drive's md5Checksum. A name already in
the target folder is never uploaded twice: same md5 -> skipped, different
md5 -> its content is replaced (only expected after a crash). Results go to
an append-only ledger (data/market/.upload_ledger.jsonl), so a restart
resumes where it stopped and a day already uploaded is never re-sent.

Also: local retention (delete uploaded days older than local_keep_days) and
the Drive budget, measured on the recorder's OWN files (not the Google
account's total, which includes Gmail/Photos): over budget, the ticks/depth
of the oldest days are deleted first.
"""

import hashlib
import json
import logging
import shutil
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pyarrow.parquet as pq

from service.marketRecorder.driveClient import FOLDER_MIME, UploadCancelled, http_status
from service.marketRecorder.fileNames import (  # noqa: F401  (names re-exported for callers)
    CANDLES_MARKER_NAME, FOLDER_CACHE_NAME, JOURNAL_DIR, LEDGER_NAME, MANIFEST_NAME, PRUNED_NAME,
)
from service.marketRecorder.tradingCalendar import day_folder_name, parse_day_folder

logger = logging.getLogger(__name__)

PARQUET_MIME = "application/vnd.apache.parquet"
PRUNABLE_STREAMS = ("ticks", "depth")


def md5_of(path: Path) -> str:
    digest = hashlib.md5()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class UploadLedger:

    def __init__(self, path: Path):
        self._path = Path(path)
        self.files: dict[str, dict] = {}   # relative path -> last record
        self.days: dict[str, dict] = {}    # DD-MM-YYYY -> last record
        self._load()

    def _load(self) -> None:
        try:
            if not self._path.is_file():
                return
            for line in self._path.read_text(encoding="utf-8").splitlines():
                try:
                    record = json.loads(line)
                    if record.get("kind") == "file":
                        self.files[record["path"]] = record
                    elif record.get("kind") == "day":
                        self.days[record["day"]] = record
                except Exception:
                    continue   # a torn last line from a crash
        except Exception as exc:
            logger.error(f"[Drive] Upload ledger {self._path} unreadable ({exc!r}) - starting empty")

    def append(self, record: dict) -> None:
        """Raises if the ledger can't be written - the upload must not be
        reported as done without its record."""
        record = {**record, "at": datetime.now().astimezone().isoformat(timespec="seconds")}
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
        if record["kind"] == "file":
            self.files[record["path"]] = record
        else:
            self.days[record["day"]] = record

    def day_done(self, day_name: str) -> bool:
        try:
            return self.days.get(day_name, {}).get("status") in ("uploaded", "skipped_empty")
        except Exception:
            return False


class DayUploader:

    def __init__(self, data_dir: Path, drive_root: str, client_factory, recorder_version: str = "",
                 stop_event: threading.Event | None = None):
        """client_factory() -> DriveClient (created lazily, so a missing token
        only matters when an upload actually runs)."""
        self._data_dir = Path(data_dir)
        self._root_name = drive_root
        self._client_factory = client_factory
        self._client = None
        self._recorder_version = recorder_version
        self._stop_event = stop_event or threading.Event()
        self.ledger = UploadLedger(self._data_dir / LEDGER_NAME)
        self._folder_cache_path = self._data_dir / FOLDER_CACHE_NAME
        self._folder_ids: dict[str, str] = self._load_folder_cache()
        self._validated_folders: set[str] = set()   # cached ids confirmed to exist in this process
        self.last_result: dict | None = None

    # -- Drive folders -----------------------------------------------------

    def _drive(self):
        if self._client is None:
            self._client = self._client_factory()
        return self._client

    def _check_stop(self) -> None:
        if self._stop_event.is_set():
            raise UploadCancelled("upload stopped")

    def _load_folder_cache(self) -> dict[str, str]:
        try:
            data = json.loads(self._folder_cache_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save_folder_cache(self) -> None:
        try:
            self._folder_cache_path.parent.mkdir(parents=True, exist_ok=True)
            self._folder_cache_path.write_text(json.dumps(self._folder_ids, indent=1), encoding="utf-8")
        except Exception as exc:
            logger.warning(f"[Drive] Folder id cache not saved: {exc!r}")

    def _forget_folders(self, path_prefix: str) -> None:
        """Drops cached ids of a folder and everything below it."""
        try:
            stale = [p for p in self._folder_ids if p == path_prefix or p.startswith(path_prefix + "/")]
            for path in stale:
                self._folder_ids.pop(path, None)
                self._validated_folders.discard(path)
            if stale:
                self._save_folder_cache()
        except Exception as exc:
            logger.warning(f"[Drive] Folder cache cleanup failed: {exc!r}")

    def _folder_id(self, parts: list[str]) -> str:
        """Id of root/part1/part2..., creating what is missing. The recorder
        must create every folder itself: with drive.file a hand-made folder
        is invisible to it. A cached id is checked once per process (not per
        file), so a folder deleted in Drive is found again or recreated."""
        parent_id = None
        for depth in range(1, len(parts) + 1):
            path = "/".join(parts[:depth])
            cached = self._folder_ids.get(path)
            if cached and (path in self._validated_folders or self._drive().folder_exists(cached)):
                self._validated_folders.add(path)
                parent_id = cached
                continue
            if cached:
                self._forget_folders(path)
            existing = self._drive().find_child(parent_id, parts[depth - 1], folder=True)
            parent_id = existing["id"] if existing else self._drive().create_folder(parent_id, parts[depth - 1])
            self._folder_ids[path] = parent_id
            self._validated_folders.add(path)
            self._save_folder_cache()
        return parent_id

    # -- one file ------------------------------------------------------------

    def _upload_file(self, local: Path, folder_parts: list[str], mime_type: str) -> dict:
        """Uploads one file (idempotent). Raises on failure."""
        self._check_stop()
        relative = local.relative_to(self._data_dir).as_posix()
        local_md5 = md5_of(local)
        size = local.stat().st_size
        previous = self.ledger.files.get(relative)
        if previous and previous.get("md5") == local_md5 and previous.get("verified"):
            return previous
        try:
            remote, action = self._send(local, folder_parts, mime_type, local_md5)
        except Exception as exc:
            if http_status(exc) != 404:
                raise
            # A cached folder was deleted in Drive (e.g. by budget pruning):
            # forget it and retry once into a freshly found/created folder.
            logger.warning(f"[Drive] Target folder of {relative} is gone - recreating it")
            self._forget_folders("/".join(folder_parts))
            remote, action = self._send(local, folder_parts, mime_type, local_md5)

        verified = remote.get("md5Checksum") == local_md5
        if not verified:
            logger.warning(f"[Drive] md5 mismatch after upload of {relative} - re-uploading once")
            remote = self._drive().update_content(remote["id"], local, mime_type)
            verified = remote.get("md5Checksum") == local_md5
        record = {"kind": "file", "path": relative, "file_id": remote.get("id"), "md5": local_md5,
                  "size": size, "action": action, "verified": verified}
        self.ledger.append(record)
        if not verified:
            raise IOError(f"md5 mismatch for {relative} after re-upload")
        return record

    def _send(self, local: Path, folder_parts: list[str], mime_type: str, local_md5: str) -> tuple[dict, str]:
        folder_id = self._folder_id(folder_parts)
        existing = self._drive().find_child(folder_id, local.name)
        if existing and existing.get("md5Checksum") == local_md5:
            return existing, "already_present"
        if existing:
            if local.name == MANIFEST_NAME:
                # Expected once: late candles add a file and update the manifest.
                logger.info(f"[Drive] Updating {'/'.join(folder_parts)}/{local.name}")
            else:
                logger.error(f"[Drive] {local.name} in {'/'.join(folder_parts)} differs from the local file - replacing its content")
            return self._drive().update_content(existing["id"], local, mime_type), "replaced"
        return self._drive().upload(folder_id, local, mime_type, expected_md5=local_md5), "uploaded"

    # -- one day ---------------------------------------------------------------

    def _day_files(self, day_dir: Path) -> list[Path]:
        try:
            return sorted(p for p in day_dir.glob("*/*.parquet") if p.is_file())
        except Exception as exc:
            logger.warning(f"[Drive] Listing {day_dir} failed: {exc!r}")
            return []

    def _write_manifest(self, day: date, files: list[Path]) -> Path:
        entries, rows_by_stream = [], {}
        for path in files:
            meta = {k.decode(): v.decode() for k, v in (pq.read_schema(path).metadata or {}).items()}
            rows = int(meta.get("row_count", 0))
            stream = path.parent.name
            rows_by_stream[stream] = rows_by_stream.get(stream, 0) + rows
            entries.append({"stream": stream, "name": path.name, "md5": md5_of(path), "size": path.stat().st_size,
                            "rows": rows, "window_start_ns": meta.get("window_start_ns"),
                            "window_end_ns": meta.get("window_end_ns")})
        # Deterministic content (no timestamp): re-running a day after a crash
        # produces the same md5, so Drive's copy is recognised, not replaced.
        manifest = {
            "trading_date": day.isoformat(), "folder": day_folder_name(day),
            "recorder_version": self._recorder_version, "files": entries, "rows_by_stream": rows_by_stream,
            # Broker candle download status and the tick-vs-candle check.
            "candles": self._candle_status(day),
        }
        path = self._data_dir / day_folder_name(day) / MANIFEST_NAME
        path.write_text(json.dumps(manifest, indent=1), encoding="utf-8")
        return path

    def _candle_status(self, day: date) -> dict | None:
        try:
            path = self._data_dir / day_folder_name(day) / CANDLES_MARKER_NAME
            if not path.is_file():
                return None
            marker = json.loads(path.read_text(encoding="utf-8"))
            marker.pop("secs", None)   # keep the manifest deterministic
            return marker
        except Exception as exc:
            return {"error": repr(exc)[:200]}

    def upload_day(self, day: date) -> dict:
        """Uploads one day. Returns a result dict; raises on failure (the
        caller keeps the local files and retries on the next run)."""
        day_name = day_folder_name(day)
        try:
            files = self._day_files(self._data_dir / day_name)
            started = time.monotonic()
            if not files:
                result = {"day": day_name, "status": "no_files"}
                self.last_result = result
                return result

            manifest_path = self._write_manifest(day, files)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest["rows_by_stream"].get("ticks", 0) == 0:
                # Nothing traded or nothing received (an unlisted holiday, a dead
                # feed all day): nothing worth keeping in Drive.
                self.ledger.append({"kind": "day", "day": day_name, "status": "skipped_empty"})
                result = {"day": day_name, "status": "skipped_empty"}
                self.last_result = result
                logger.warning(f"[Drive] {day_name}: no ticks recorded - not uploaded")
                return result

            uploaded = 0
            for path in files:
                record = self._upload_file(path, [self._root_name, day_name, path.parent.name], PARQUET_MIME)
                uploaded += record.get("action") in ("uploaded", "replaced")
            self._upload_file(manifest_path, [self._root_name, day_name], "application/json")

            total_bytes = sum(p.stat().st_size for p in files)
            self.ledger.append({"kind": "day", "day": day_name, "status": "uploaded", "files": len(files), "bytes": total_bytes,
                                "candles_status": (manifest.get("candles") or {}).get("status")})
            result = {"day": day_name, "status": "uploaded", "files": len(files), "newly_uploaded": uploaded,
                      "mb": round(total_bytes / 1e6, 1), "secs": round(time.monotonic() - started, 1),
                      "rows_by_stream": manifest["rows_by_stream"]}
            self.last_result = result
            logger.info(f"[Drive] Day {day_name} uploaded: {result}")
            return result
        except UploadCancelled:
            logger.warning(f"[Drive] Upload of {day_name} stopped by shutdown - resumes on the next run")
            raise
        except Exception as exc:
            logger.error(f"[Drive] Upload of {day_name} failed: {exc!r}")
            self.last_result = {"day": day_name, "status": "failed", "error": repr(exc)[:500]}
            raise

    # -- which days, retention -------------------------------------------------

    def local_days(self) -> list[date]:
        try:
            if not self._data_dir.is_dir():
                return []
            days = [parse_day_folder(p.name) for p in self._data_dir.iterdir() if p.is_dir()]
            return sorted(d for d in days if d is not None)
        except Exception as exc:
            logger.warning(f"[Drive] Listing local days failed: {exc!r}")
            return []

    def has_late_candles(self, day: date) -> bool:
        """The day was uploaded before its candle download finished (e.g. the
        broker was down at the close): its candles and updated manifest still
        have to go up."""
        try:
            record = self.ledger.days.get(day_folder_name(day), {})
            if record.get("status") != "uploaded":
                return False
            status = self._candle_status(day)
            return bool(status) and status.get("status") != record.get("candles_status")
        except Exception:
            return False

    def has_open_journal(self, day: date) -> bool:
        try:
            folder = self._data_dir / JOURNAL_DIR / day_folder_name(day)
            return folder.is_dir() and any(folder.rglob("*.arrows"))
        except Exception:
            return True   # unsure: don't upload a day that may still be open

    def pending_days(self, finished_before: date, include_today: date | None = None, ready=None) -> list[date]:
        """Recorded days not yet uploaded: every day before finished_before,
        plus include_today when its session is over. ready(day) -> bool can
        hold a day back (e.g. until its candles are downloaded)."""
        result = []
        try:
            for day in self.local_days():
                if day < finished_before or day == include_today:
                    name = day_folder_name(day)
                    pending = not self.ledger.day_done(name) or self.has_late_candles(day)
                    if pending and not self.has_open_journal(day):
                        if ready is None or ready(day):
                            result.append(day)
        except Exception as exc:
            logger.warning(f"[Drive] Working out pending days failed: {exc!r}")
        return result

    def prune_local(self, keep_days: int, today: date) -> list[str]:
        """Deletes local days that are verified in Drive and older than keep_days."""
        removed = []
        try:
            cutoff = today - timedelta(days=keep_days)
            for day in self.local_days():
                name = day_folder_name(day)
                if day < cutoff and self.ledger.day_done(name):
                    shutil.rmtree(self._data_dir / name, ignore_errors=True)
                    removed.append(name)
            if removed:
                logger.info(f"[Recorder] Local retention removed {removed}")
        except Exception as exc:
            logger.warning(f"[Recorder] Local retention failed: {exc!r}")
        return removed

    def _recorder_usage(self, root_id: str) -> tuple[int, list[tuple[date, dict, list[dict], dict[str, int]]]]:
        """(total bytes of the recorder's files in Drive, per day oldest first:
        (day, day folder, stream folders, bytes per stream))."""
        total, days = 0, []
        for folder in self._drive().list_children(root_id, folders_only=True):
            self._check_stop()
            day = parse_day_folder(folder.get("name", ""))
            if day is None:
                continue
            stream_folders, bytes_by_stream = [], {}
            for child in self._drive().list_children(folder["id"]):
                if child.get("mimeType") == FOLDER_MIME:
                    stream_folders.append(child)
                    size = sum(int(f.get("size") or 0) for f in self._drive().list_children(child["id"]))
                    bytes_by_stream[child["name"]] = size
                    total += size
                else:
                    total += int(child.get("size") or 0)   # the manifest
            days.append((day, folder, stream_folders, bytes_by_stream))
        days.sort(key=lambda item: item[0])
        return total, days

    def _record_pruned(self, day_folder: dict, streams: list[str]) -> None:
        """Writes/updates <day>/_pruned.json in Drive listing the streams the
        budget removed, so the audit reports them as pruned, not as missing.
        Best effort: never raises (the deletion itself already happened)."""
        local = None
        try:
            existing = self._drive().find_child(day_folder["id"], PRUNED_NAME)
            previous = []
            if existing is not None:
                try:
                    previous = json.loads(self._drive().download_text(existing["id"])).get("pruned_streams", [])
                except Exception:
                    previous = []
            content = {"pruned_streams": sorted(set(previous) | set(streams)),
                       "reason": "Drive budget (RECORDER_DRIVE_BUDGET_GB)",
                       "at": datetime.now().astimezone().isoformat(timespec="seconds")}
            local = self._data_dir / f".{day_folder['name']}{PRUNED_NAME}"
            local.write_text(json.dumps(content, indent=1), encoding="utf-8")
            if existing is not None:
                self._drive().update_content(existing["id"], local, "application/json")
            else:
                self._upload_named(day_folder["id"], local)
        except UploadCancelled:
            raise
        except Exception as exc:
            logger.warning(f"[Drive] Could not record pruning of {day_folder.get('name')}: {exc!r}")
        finally:
            try:
                if local is not None:
                    local.unlink(missing_ok=True)
            except OSError:
                pass

    def _upload_named(self, folder_id: str, local: Path) -> None:
        """Uploads a temp file under the name _pruned.json."""
        named = local.parent / ".tmp_upload" / PRUNED_NAME
        try:
            named.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(local, named)
            self._drive().upload(folder_id, named, "application/json", expected_md5=md5_of(named))
        finally:
            try:
                named.unlink(missing_ok=True)
            except OSError:
                pass

    def enforce_drive_budget(self, budget_gb: float, prune_after_days: int, today: date) -> dict:
        """When the recorder's own data in Drive is over budget, deletes
        ticks/depth of the oldest days older than prune_after_days (chain,
        events, instruments and the manifest are kept longest). The Google
        account's total usage is only reported, never used to delete."""
        status = {"budget_gb": budget_gb, "pruned": []}
        try:
            try:
                account_usage, limit = self._drive().storage_usage_bytes()
                status["account_usage_gb"] = round(account_usage / 1e9, 2)
                status["account_limit_gb"] = round(limit / 1e9, 2) if limit else None
                if limit and account_usage > 0.95 * limit:
                    logger.warning(f"[Drive] The Google account is {account_usage / limit:.0%} full - uploads may start failing")
            except UploadCancelled:
                raise
            except Exception as exc:
                status["account_usage_error"] = repr(exc)[:200]

            root_id = self._folder_id([self._root_name])
            usage, days = self._recorder_usage(root_id)
            status["recorder_usage_gb"] = round(usage / 1e9, 3)
            budget = budget_gb * 1e9
            cutoff = today - timedelta(days=prune_after_days)
            for day, folder, stream_folders, bytes_by_stream in days:
                if usage <= budget or day >= cutoff:
                    break
                pruned_here = []
                for stream_folder in stream_folders:
                    if stream_folder["name"] not in PRUNABLE_STREAMS:
                        continue
                    self._check_stop()
                    self._drive().delete(stream_folder["id"])
                    self._forget_folders(f"{self._root_name}/{folder['name']}/{stream_folder['name']}")
                    freed = bytes_by_stream.get(stream_folder["name"], 0)
                    usage -= freed
                    pruned_here.append(stream_folder["name"])
                    status["pruned"].append(f"{folder['name']}/{stream_folder['name']}")
                    logger.warning(f"[Drive] Over budget: deleted {folder['name']}/{stream_folder['name']} ({freed / 1e6:.0f} MB)")
                if pruned_here:
                    self._record_pruned(folder, pruned_here)
            status["recorder_usage_after_gb"] = round(usage / 1e9, 3)
            if usage > budget:
                logger.warning(f"[Drive] Recorder data is {usage / 1e9:.2f} GB, over the {budget_gb} GB budget, "
                               f"and nothing older than {prune_after_days} days is left to prune")
        except UploadCancelled:
            raise
        except Exception as exc:
            logger.warning(f"[Drive] Budget check failed: {exc!r}")
            status["error"] = repr(exc)[:300]
        return status
