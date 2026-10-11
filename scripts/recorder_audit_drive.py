"""
Audits the recorder's day folders in Google Drive for consistency.

    python scripts/recorder_audit_drive.py                 # the real folder (RECORDER_DRIVE_ROOT, PrimePipData)
    python scripts/recorder_audit_drive.py --root PrimePipData_TEST

Exit code 1 if any day has a problem.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from service.marketRecorder.config import load_recorder_config  # noqa: E402
from service.marketRecorder.driveAudit import DriveAudit  # noqa: E402
from service.marketRecorder.driveClient import DriveClient  # noqa: E402
from service.marketRecorder.tradingCalendar import TradingCalendar  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", help="Drive root folder (default: RECORDER_DRIVE_ROOT)")
    args = parser.parse_args()
    config = load_recorder_config()
    client = DriveClient(config.drive_token_file, config.drive_account)
    calendar = TradingCalendar(config.session_start, config.session_end, config.window_minutes)
    report = DriveAudit(client, args.root or config.drive_root, calendar).audit()

    print(f"Drive account {client.account}, folder {report['root']}: {len(report['days'])} day folder(s)")
    if report.get("error"):
        print("ERROR:", report["error"])
    for day, result in report["days"].items():
        status = "OK" if not result["problems"] else "PROBLEM"
        print(f"  {day}: {status} - {result.get('files')} files, rows {result.get('rows_by_stream', {})}")
        for problem in result["problems"]:
            print(f"      problem: {problem}")
        for note in result["notes"]:
            print(f"      note: {note}")
    print("All day folders consistent." if report["ok"] else "Inconsistencies found.")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"Audit failed: {exc!r}")
        sys.exit(1)
