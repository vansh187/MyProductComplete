import json
from datetime import date, datetime
from pathlib import Path
import logging

from appconfig.ScripMasterRefresher import ScripMasterRefresher, underlying_items, upcoming_expiry_dates

logger = logging.getLogger(__name__)

_FILE = Path(__file__).parent / "master_futures.json"


def _load(path: Path) -> dict[str, dict] | None:
    """
    Structure in JSON: { "NIFTY": { "expiries": [...], "<iso-expiry>":
    {"token", "tsym", "lot_size"} }, ... } - one contract per (underlying,
    expiry), unlike OptionMaster's per-strike CE/PE pair.

    A missing/corrupt file must never take down the whole app at import time
    (mirrors appconfig/OptionMaster.py's own reasoning) - futures
    classification simply falls back to the symbol-suffix heuristic in
    utils/instrumentClassifier.looks_like_future_symbol until
    `python scripts/build_future_master.py` is run to generate it.
    Returns None (never raises) when unreadable - see OptionMaster._load.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning(f"[FutureMaster] {path} missing or unreadable ({exc}) - futures "
            f"classification falls back to the symbol-suffix heuristic until a refresh succeeds")
        return None
    if not isinstance(data, dict):
        logger.warning(f"[FutureMaster] {path} is not a JSON object - ignoring it")
        return None
    return data


# Sensex futures trade on Shoonya's separate BFO segment; every other tracked
# underlying (NIFTY, BANKNIFTY, FINNIFTY) is NFO. Mirrors _BFO_UNDERLYINGS in
# appconfig/OptionMaster.py and BFO_UNDERLYINGS in scripts/build_future_master.py.
_BFO_UNDERLYINGS = {"SENSEX"}


def _build_tsym_index(raw: dict) -> dict[str, dict]:
    """TSYM -> contract, so find_by_tsym is a dict hit on the order path
    instead of a scan of every contract. First occurrence wins, matching the
    old first-match scan."""
    index: dict[str, dict] = {}
    try:
        for underlying, expiries in underlying_items(raw):
            exchange = "BFO" if underlying in _BFO_UNDERLYINGS else "NFO"
            for expiry, info in expiries.items():
                if expiry == "expiries" or not isinstance(info, dict):
                    continue
                tsym = str(info.get("tsym") or "").upper()
                if not tsym or "token" not in info:
                    continue
                index.setdefault(tsym, {
                    "token": info["token"],
                    "lot_size": info.get("lot_size"),
                    "exchange": exchange,
                    "underlying": underlying,
                    "expiry": expiry,
                })
    except Exception as exc:
        logger.error(f"[FutureMaster] Failed to build tsym index: {exc}", exc_info=True)
    return index


_raw: dict[str, dict] = _load(_FILE) or {}
_tsym_index: dict[str, dict] = _build_tsym_index(_raw)


def _lookup_key(value) -> str | None:
    """Normalized lookup key; None for non-text input, so the accessors
    below answer "not found" instead of raising."""
    if not isinstance(value, str):
        return None
    key = value.strip().upper()
    return key or None


def _contracts_for(underlying) -> dict:
    contracts = _raw.get(_lookup_key(underlying))
    return contracts if isinstance(contracts, dict) else {}


def is_valid_underlying(underlying: str) -> bool:
    return bool(_contracts_for(underlying))


def get_expiries(underlying: str) -> list[str]:
    """All listed expiries (ISO 'YYYY-MM-DD', ascending) for an underlying."""
    expiries = _contracts_for(underlying).get("expiries")
    return expiries if isinstance(expiries, list) else []


def nearest_expiry(underlying: str, today: date | None = None, now: datetime | None = None) -> str | None:
    """Earliest still-tradable expiry, or None if none is left - same
    expiry-day close rule as OptionMaster (ScripMasterRefresher.upcoming_expiry_dates)."""
    upcoming = upcoming_expiry_dates(get_expiries(underlying), today=today, now=now)
    return upcoming[0] if upcoming else None


def find_by_tsym(tsym: str) -> dict | None:
    """
    Reverse lookup for order placement and position building: tradingsymbol
    (e.g. 'NIFTY28JUL26F') -> {token, lot_size, exchange, underlying, expiry}.
    Returns a copy, so callers may mutate it freely.
    """
    contract = _tsym_index.get(_lookup_key(tsym))
    return dict(contract) if contract is not None else None


def _install(raw: dict) -> None:
    """Builds the index for `raw` first, then swaps both globals."""
    global _raw, _tsym_index
    index = _build_tsym_index(raw)
    _raw, _tsym_index = raw, index


def reload() -> bool:
    """Re-reads master_futures.json from disk without restarting the process.
    An unreadable file keeps the current in-memory master."""
    raw = _load(_FILE)
    if raw is None:
        logger.error("[FutureMaster] reload: file unreadable - keeping the current in-memory master")
        return False
    _install(raw)
    return True


def _download_from_broker() -> dict:
    from scripts.build_future_master import download_future_master
    return download_future_master()


_refresher = ScripMasterRefresher(
    name="FutureMaster",
    file_path=_FILE,
    download=_download_from_broker,
    install=_install,
    current_master=lambda: _raw,
)


async def schedule_daily_refresh(app=None) -> None:
    """
    Background task (started from app.py lifespan): refreshes immediately when
    the master is stale, then re-downloads Shoonya's NFO/BFO futures scrip
    masters daily at 08:15 IST, since expiries roll over monthly/quarterly and
    tokens occasionally get reshuffled. Same schedule and staleness rules as
    appconfig/OptionMaster.py (see appconfig/ScripMasterRefresher.py).
    """
    await _refresher.run_forever()
