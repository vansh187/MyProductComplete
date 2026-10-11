"""
Keeps the recorder's holiday list current from NSE's own holiday API
(https://www.nseindia.com/api/holiday-master?type=trading), F&O segment.

The fetched list is written to a runtime cache file (data/market/
.nse_holidays.json, git-ignored) - never to the tracked base list in
appconfig/, so the VM's checkout stays clean and hand-added dates are never
overwritten. Within the cache, only the calendar years NSE returned are
replaced; dates of other years are kept. The calendar uses base + cache.

NSE rejects requests without browser-like headers and its session cookies,
so the homepage is visited first (it may answer 403; the cookies it sets are
still what the API wants). On any failure the saved lists stay in use.
"""

import json
import logging
import os
from datetime import datetime
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

HOME_URL = "https://www.nseindia.com/"
API_URL = "https://www.nseindia.com/api/holiday-master?type=trading"
REFERER = "https://www.nseindia.com/resources/exchange-communication-holidays"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}
TIMEOUT_SECS = 15
SEGMENT = "FO"


def parse_holidays(payload: dict, segment: str = SEGMENT) -> list[dict]:
    """[{date: 'YYYY-MM-DD', day, description, special}] from the API payload.
    Unparseable rows are skipped; never raises."""
    result = []
    try:
        items = payload.get(segment, []) or []
    except Exception:
        return result
    for item in items:
        try:
            day = datetime.strptime(item["tradingDate"], "%d-%b-%Y").date()
            description = str(item.get("description") or "").strip()
            result.append({"date": day.isoformat(), "day": item.get("weekDay"),
                           "description": description.rstrip("*").strip(), "special": description.endswith("*")})
        except Exception:
            continue
    return sorted(result, key=lambda h: h["date"])


def fetch_holidays(session: requests.Session | None = None) -> list[dict]:
    """Raises on network/HTTP errors - refresh_holiday_file handles them."""
    session = session or requests.Session()
    session.headers.update(HEADERS)
    try:
        session.get(HOME_URL, timeout=TIMEOUT_SECS)   # cookies only; the status doesn't matter
    except Exception:
        pass
    response = session.get(API_URL, timeout=TIMEOUT_SECS, headers={"Accept": "application/json", "Referer": REFERER})
    response.raise_for_status()
    return parse_holidays(response.json())


def merge_by_year(existing: dict, fetched: list[dict]) -> tuple[list[str], dict[str, str]]:
    """Replaces the years NSE returned, keeps every other year's dates."""
    fetched_years = {h["date"][:4] for h in fetched}
    old_names = existing.get("holiday_names", {}) if isinstance(existing.get("holiday_names"), dict) else {}
    kept = [str(d) for d in existing.get("holidays", []) if str(d)[:4] not in fetched_years]
    names = {d: old_names.get(d, "") for d in kept}
    for holiday in fetched:
        names[holiday["date"]] = holiday["description"] + (" (special session)" if holiday["special"] else "")
    return sorted(set(kept) | {h["date"] for h in fetched}), dict(sorted(names.items()))


def refresh_holiday_file(path: Path, session: requests.Session | None = None) -> bool:
    """Updates the runtime holiday cache at `path` from NSE. True if it was
    updated. Never raises."""
    try:
        path = Path(path)
        try:
            holidays = fetch_holidays(session)
        except Exception as exc:
            logger.warning(f"[Recorder] NSE holiday list not refreshed ({exc!r}) - keeping the saved list")
            return False
        if not holidays:
            logger.warning("[Recorder] NSE holiday API returned no F&O holidays - keeping the saved list")
            return False
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
            existing = existing if isinstance(existing, dict) else {}
        except Exception:
            existing = {}
        dates, names = merge_by_year(existing, holidays)
        data = {
            "_comment": "Fetched automatically from NSE by the market data recorder. Runtime file - do not edit; "
                        "add dates by hand to appconfig/nse_holidays.json instead.",
            "source": API_URL,
            "fetched_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "holidays": dates,
            "holiday_names": names,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
        logger.info(f"[Recorder] NSE holiday list refreshed: {len(holidays)} dates fetched, {len(dates)} saved")
        return True
    except Exception as exc:
        logger.warning(f"[Recorder] Holiday cache update failed: {exc!r}")
        return False
