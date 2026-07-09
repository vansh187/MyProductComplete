import asyncio
import json
from datetime import date, datetime
from pathlib import Path

_FILE = Path(__file__).parent / "master_options.json"


def _load(path: Path) -> dict[str, dict]:
    """
    Structure in JSON: { "NIFTY": { "expiries": [...], "<iso-expiry>": { "<strike>": {...} } }, ... }

    A missing/corrupt file must never take down the whole app at import time
    (app.py imports api/optionChain.py -> this module unconditionally at
    startup) - the rest of the platform (orders, wallet, candles, etc.) has
    nothing to do with the option chain feature and must keep working. Falls
    back to an empty chain set instead: is_valid_underlying()/get_strike_chain()
    then simply report "nothing available" and the option-chain endpoints
    return no_option_data rather than crashing the server.
    """
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        print(
            f"[OptionMaster] {path} missing or unreadable ({exc}) - option chain "
            f"will report no_option_data until `python scripts/build_option_master.py` "
            f"is run to generate it."
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


def get_strike_chain(underlying: str, expiry: str) -> dict[str, dict]:
    """strike (str) -> {ce_token, ce_tsym, pe_token, pe_tsym, lot_size} for one (underlying, expiry)."""
    return _raw.get(underlying.upper(), {}).get(expiry, {})


def reload() -> None:
    """Re-reads master_options.json from disk without restarting the process."""
    global _raw
    _raw = _load(_FILE)


async def schedule_daily_refresh(app=None) -> None:
    """
    Background task (started from app.py lifespan): re-downloads Shoonya's
    NFO scrip master once a day, since new expiries/contracts get added and
    tokens occasionally get reshuffled.
    """
    from scripts.build_option_master import download_option_master

    while True:
        await asyncio.sleep(24 * 3600)
        try:
            loop = asyncio.get_running_loop()
            chains = await loop.run_in_executor(None, download_option_master)
            _FILE.write_text(json.dumps(chains, indent=2, ensure_ascii=False), encoding="utf-8")
            reload()
            print("[OptionMaster] Refreshed NFO scrip master")
        except Exception as exc:
            print(f"[OptionMaster] Refresh failed: {exc}")
