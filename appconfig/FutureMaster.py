import asyncio
import json
from datetime import date, datetime
from pathlib import Path

_FILE = Path(__file__).parent / "master_futures.json"


def _load(path: Path) -> dict[str, dict]:
    """
    Structure in JSON: { "NIFTY": { "expiries": [...], "<iso-expiry>":
    {"token", "tsym", "lot_size"} }, ... } - one contract per (underlying,
    expiry), unlike OptionMaster's per-strike CE/PE pair.

    A missing/corrupt file must never take down the whole app at import time
    (mirrors appconfig/OptionMaster.py's own reasoning) - futures
    classification simply falls back to the symbol-suffix heuristic in
    utils/instrumentClassifier.looks_like_future_symbol until
    `python scripts/build_future_master.py` is run to generate it.
    """
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        print(
            f"[FutureMaster] {path} missing or unreadable ({exc}) - futures "
            f"classification will fall back to the symbol-suffix heuristic "
            f"until `python scripts/build_future_master.py` is run to generate it."
        )
        return {}


_raw: dict[str, dict] = _load(_FILE)


def is_valid_underlying(underlying: str) -> bool:
    return underlying.upper() in _raw


def get_expiries(underlying: str) -> list[str]:
    """All available expiries (ISO 'YYYY-MM-DD', ascending) for an underlying."""
    return _raw.get(underlying.upper(), {}).get("expiries", [])


def nearest_expiry(underlying: str, today: date | None = None) -> str | None:
    """Earliest available expiry >= today (IST calendar date), or None if none left."""
    today = today or datetime.now().date()
    for expiry in get_expiries(underlying):
        if datetime.strptime(expiry, "%Y-%m-%d").date() >= today:
            return expiry
    return None


# Sensex futures trade on Shoonya's separate BFO segment; every other tracked
# underlying (NIFTY, BANKNIFTY, FINNIFTY) is NFO. Mirrors _BFO_UNDERLYINGS in
# appconfig/OptionMaster.py and BFO_UNDERLYINGS in scripts/build_future_master.py.
_BFO_UNDERLYINGS = {"SENSEX"}


def find_by_tsym(tsym: str) -> dict | None:
    """
    Reverse lookup for order placement and position building: tradingsymbol
    (e.g. 'NIFTY28JUL26F') -> {token, lot_size, exchange, underlying, expiry}.
    Tradingsymbols are unique per contract, so this scans every underlying/
    expiry without needing to parse the symbol string.
    """
    if not tsym:
        return None
    tsym = tsym.upper().strip()
    for underlying, expiries in _raw.items():
        for expiry, info in expiries.items():
            if expiry == "expiries":
                continue
            if info.get("tsym", "").upper() != tsym:
                continue
            return {
                "token": info["token"],
                "lot_size": info["lot_size"],
                "exchange": "BFO" if underlying in _BFO_UNDERLYINGS else "NFO",
                "underlying": underlying,
                "expiry": expiry,
            }
    return None


def reload() -> None:
    """Re-reads master_futures.json from disk without restarting the process."""
    global _raw
    _raw = _load(_FILE)


async def schedule_daily_refresh(app=None) -> None:
    """
    Background task (started from app.py lifespan): re-downloads Shoonya's
    NFO/BFO futures scrip masters once a day, since expiries roll over
    monthly/quarterly and tokens occasionally get reshuffled. Mirrors
    appconfig/OptionMaster.schedule_daily_refresh().
    """
    from scripts.build_future_master import download_future_master

    while True:
        await asyncio.sleep(24 * 3600)
        try:
            loop = asyncio.get_running_loop()
            contracts = await loop.run_in_executor(None, download_future_master)
            _FILE.write_text(json.dumps(contracts, indent=2, ensure_ascii=False), encoding="utf-8")
            reload()
            print("[FutureMaster] Refreshed futures scrip master")
        except Exception as exc:
            print(f"[FutureMaster] Refresh failed: {exc}")
