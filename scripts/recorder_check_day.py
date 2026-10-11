"""
Checks one recorded day against the data contract and prints a summary.

    python scripts/recorder_check_day.py 12-10-2026
    python scripts/recorder_check_day.py 12-10-2026 --upload   # upload it to Drive now

Exit code 1 if any file fails validation.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

import pyarrow.parquet as pq  # noqa: E402

from service.marketRecorder.config import load_recorder_config  # noqa: E402
from service.marketRecorder.contract import validate_file  # noqa: E402
from service.marketRecorder.fileNames import CANDLES_MARKER_NAME  # noqa: E402
from service.marketRecorder.tradingCalendar import TradingCalendar, parse_day_folder  # noqa: E402
from service.marketRecorder.uploadScheduler import UploadScheduler  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("day", help="day folder name, DD-MM-YYYY")
    parser.add_argument("--upload", action="store_true", help="upload the day to Google Drive after checking")
    args = parser.parse_args()

    config = load_recorder_config()
    day = parse_day_folder(args.day)
    if day is None:
        print("Day must be DD-MM-YYYY")
        return 2
    day_dir = config.data_dir / args.day
    if not day_dir.is_dir():
        print(f"No such folder: {day_dir}")
        return 2

    calendar = TradingCalendar(config.session_start, config.session_end, config.window_minutes)
    expected = {w.file_name for w in calendar.windows(day)}
    failed = False
    total_bytes = 0
    for stream_dir in sorted(p for p in day_dir.iterdir() if p.is_dir()):
        files = sorted(stream_dir.glob("*.parquet"))
        rows = 0
        for path in files:
            errors = validate_file(path)
            total_bytes += path.stat().st_size
            rows += pq.read_metadata(path).num_rows
            if errors:
                failed = True
                print(f"  FAIL {stream_dir.name}/{path.name}: {errors}")
        daily = stream_dir.name in ("instruments", "candles")   # one file a day, not hourly
        missing = sorted(expected - {p.name for p in files}) if not daily else []
        print(f"{stream_dir.name:12} {len(files):2} files {rows:>10,} rows" + (f"   missing: {missing}" if missing else ""))
    print(f"Total {total_bytes / 1e6:.1f} MB - {'FAILED' if failed else 'all files valid'}")
    marker = day_dir / CANDLES_MARKER_NAME
    if marker.is_file():
        import json
        status = json.loads(marker.read_text(encoding="utf-8"))
        print(f"Candles: {status.get('status')}, {status.get('candles')} candles for {status.get('instruments')} instruments; "
              f"tick check {status.get('check')}")
    else:
        print("Candles: not downloaded yet")

    if args.upload and not failed:
        scheduler = UploadScheduler(config)
        print(scheduler.uploader.upload_day(day))
        scheduler.close()
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"Check failed: {exc!r}")
        sys.exit(1)
