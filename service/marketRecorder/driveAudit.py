"""
Consistency audit of the recorder's data in Google Drive:

  <root>/DD-MM-YYYY/<stream>/part-HHMM.parquet   (ticks, depth, chain, events)
  <root>/DD-MM-YYYY/instruments/snapshot.parquet
  <root>/DD-MM-YYYY/candles/candles_1m.parquet   (when that day's candles were downloaded)
  <root>/DD-MM-YYYY/_manifest.json

Per day folder it checks: the folder name is a date; the manifest exists (the
day is complete); every file the manifest lists is in Drive exactly once
with the same md5; no unexpected or duplicate files; and which hourly
windows each stream has (a missing window is reported - e.g. the recorder
started late - not hidden).
"""

import json
import logging
from datetime import date

from service.marketRecorder import contract
from service.marketRecorder.driveClient import FOLDER_MIME
from service.marketRecorder.fileNames import (
    CANDLES_DIR, CANDLES_FILE, INSTRUMENTS_DIR, INSTRUMENTS_FILE, MANIFEST_NAME, PRUNED_NAME,
)
from service.marketRecorder.tradingCalendar import TradingCalendar, parse_day_folder

logger = logging.getLogger(__name__)

_INSTRUMENTS_PATH = f"{INSTRUMENTS_DIR}/{INSTRUMENTS_FILE}"
_CANDLES_PATH = f"{CANDLES_DIR}/{CANDLES_FILE}"


class DriveAudit:

    def __init__(self, client, root_name: str, calendar: TradingCalendar):
        self._client = client
        self._root_name = root_name
        self._calendar = calendar

    def audit(self) -> dict:
        """{"root": ..., "days": {DD-MM-YYYY: {...}}, "ok": bool}. Never raises."""
        report = {"root": self._root_name, "days": {}, "ok": True}
        try:
            root = self._client.find_child(None, self._root_name, folder=True)
            if root is None:
                report.update(ok=False, error=f"folder {self._root_name} not found in Drive")
                return report
            for folder in sorted(self._client.list_children(root["id"]),
                                 key=lambda f: parse_day_folder(f.get("name", "")) or date.max):
                day_report = self._audit_day(folder)
                report["days"][folder.get("name")] = day_report
                report["ok"] = report["ok"] and not day_report["problems"]
        except Exception as exc:
            logger.error(f"[Drive] Audit failed: {exc!r}")
            report.update(ok=False, error=repr(exc)[:300])
        return report

    def _audit_day(self, folder: dict) -> dict:
        problems, notes = [], []
        name = folder.get("name", "")
        day = parse_day_folder(name)
        if folder.get("mimeType") != FOLDER_MIME or day is None:
            return {"problems": [f"'{name}' is not a DD-MM-YYYY day folder"], "notes": [], "files": 0}
        try:
            remote: dict[str, list[dict]] = {}
            for child in self._client.list_children(folder["id"]):
                if child.get("mimeType") == FOLDER_MIME:
                    for item in self._client.list_children(child["id"]):
                        remote.setdefault(f"{child['name']}/{item['name']}", []).append(item)
                else:
                    remote.setdefault(child["name"], []).append(child)

            duplicates = sorted(path for path, items in remote.items() if len(items) > 1)
            if duplicates:
                problems.append(f"duplicate files: {duplicates}")

            manifest_items = remote.get(MANIFEST_NAME)
            if not manifest_items:
                problems.append("no _manifest.json - the day's upload is incomplete")
                return {"problems": problems, "notes": notes, "files": len(remote)}
            manifest = json.loads(self._client.download_text(manifest_items[0]["id"]))
            pruned_streams = set()
            if remote.get(PRUNED_NAME):
                try:
                    pruned_streams = set(json.loads(self._client.download_text(remote[PRUNED_NAME][0]["id"]))
                                         .get("pruned_streams", []))
                except Exception as exc:
                    problems.append(f"{PRUNED_NAME} unreadable: {exc!r}"[:200])

            expected = {f"{entry['stream']}/{entry['name']}": entry["md5"] for entry in manifest.get("files", [])}
            missing_all = sorted(path for path in expected if path not in remote)
            # Removed on purpose by the Drive budget (recorded in _pruned.json): a note, not a problem.
            pruned = [path for path in missing_all if path.split("/", 1)[0] in pruned_streams]
            missing = [path for path in missing_all if path not in pruned]
            if pruned:
                notes.append(f"pruned by the Drive budget: {sorted(pruned_streams)} ({len(pruned)} files)")
            if missing:
                problems.append(f"listed in the manifest but not in Drive: {missing}")
            wrong_md5 = sorted(path for path, md5 in expected.items()
                               if path in remote and remote[path][0].get("md5Checksum") != md5)
            if wrong_md5:
                problems.append(f"md5 differs from the manifest: {wrong_md5}")
            unexpected = sorted(path for path in remote if path not in (MANIFEST_NAME, PRUNED_NAME) and path not in expected)
            if unexpected:
                problems.append(f"in Drive but not in the manifest: {unexpected}")

            if manifest.get("trading_date") != day.isoformat():
                problems.append(f"manifest trading_date {manifest.get('trading_date')} != folder {day.isoformat()}")

            all_windows = [w.file_name for w in self._calendar.windows(day)]
            for stream in contract.WINDOWED_SCHEMAS:
                if stream in pruned_streams:
                    continue
                present = sorted(path.split("/", 1)[1] for path in expected if path.startswith(f"{stream}/"))
                absent = [w for w in all_windows if w not in present]
                if absent:
                    notes.append(f"{stream}: no file for {absent} (recorder not running then - see events)")
            if _INSTRUMENTS_PATH not in expected:
                problems.append("no instruments snapshot")
            candles = manifest.get("candles") or {}
            if candles.get("status") in ("complete", "partial") and _CANDLES_PATH not in expected:
                problems.append("manifest says candles were downloaded but the file is missing")
            if candles.get("status") in ("partial", "failed"):
                notes.append(f"candles {candles['status']}: missing for {len(candles.get('failed', []))} instruments")
            if not candles:
                notes.append("no broker candles for this day")
            elif candles.get("check", {}).get("missing_unexplained"):
                notes.append(f"tick/candle check: {candles['check']['missing_unexplained']} unexplained minutes")

            return {"problems": problems, "notes": notes, "files": len(remote),
                    "rows_by_stream": manifest.get("rows_by_stream", {})}
        except Exception as exc:
            return {"problems": [f"audit error: {exc!r}"[:300]], "notes": notes, "files": None}
