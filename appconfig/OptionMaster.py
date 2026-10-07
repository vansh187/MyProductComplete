import json
from datetime import date, datetime
from pathlib import Path
import logging

from appconfig.ScripMasterRefresher import (
    ScripMasterRefresher,
    is_master_stale,
    today_ist as _today_ist,
    underlying_items,
    upcoming_expiry_dates,
)
from utils.safe_numbers import safe_float

logger = logging.getLogger(__name__)

_FILE = Path(__file__).parent / "master_options.json"

# Sensex options trade on Shoonya's separate BFO segment; every other tracked
# underlying (NIFTY, BANKNIFTY, FINNIFTY) is NFO. Mirrors BFO_UNDERLYINGS in
# scripts/build_option_master.py.
_BFO_UNDERLYINGS = {"SENSEX"}

# Index options tick size; used only for a master built before tick_size
# was recorded per strike.
DEFAULT_TICK_SIZE = 0.05


def _load(path: Path) -> dict[str, dict] | None:
    """
    Structure in JSON: { "NIFTY": { "expiries": [...], "<iso-expiry>": { "<strike>": {...} } }, ... }

    Returns None (never raises) for a missing/corrupt file, so callers can
    tell "unreadable" apart from a real master: at import time that becomes
    an empty master (the rest of the platform must still start; the option
    chain reports no_option_data and the startup refresh downloads a fresh
    one), while reload() keeps whatever is already in memory.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning(f"[OptionMaster] {path} missing or unreadable ({exc})")
        return None
    if not isinstance(data, dict):
        logger.warning(f"[OptionMaster] {path} is not a JSON object - ignoring it")
        return None
    return data


def _suffix_convention_alias(native_tsym: str, strike: str, infix_letter: str, suffix: str) -> str:
    """
    Converts Shoonya's native '{prefix}{C|P}{strike}' tradingsymbol (e.g.
    'NIFTY14JUL26C23950', as stored in master_options.json / returned by
    build_option_master.py) into the alternate '{prefix}{strike}{CE|PE}'
    convention (e.g. 'NIFTY14JUL2623950CE') that OrderCreate's own field
    description and callers historically assumed - these are NOT the same
    string, and find_by_tsym() previously only matched the native form, so
    any order/position lookup using the CE/PE-suffix convention silently
    found nothing: token/exchange never resolved at order creation
    (service/orderService.py), underlying/expiry/strike/option_type stayed
    None at position building (service/positionService.py), and
    margin_engine.resolve_contract_type() couldn't even recognize the
    order as an OPTION - meaning margin could go unblocked entirely for an
    option order using this convention. Returns "" (never matches) if
    native_tsym doesn't end with infix_letter+strike as expected.
    """
    marker = infix_letter + strike
    if not native_tsym.endswith(marker):
        return ""
    prefix = native_tsym[: -len(marker)]
    return f"{prefix}{strike}{suffix}"


def _build_indexes(raw: dict) -> tuple[dict[str, tuple[dict, list[str]]], dict[str, list[str]]]:
    """
    One pass over the master building two hash indexes, so the per-order and
    per-tick reverse lookups below are O(1) instead of a scan of every
    contract (~10k legs) on every call:
      tsym_index:  TSYM (native AND CE/PE-suffix alias) -> (contract dict, [native_tsym, alias])
      token_index: token -> [native_tsym, alias] (native first)
    setdefault keeps the first occurrence, matching the old first-match scan.
    """
    tsym_index: dict[str, tuple[dict, list[str]]] = {}
    token_index: dict[str, list[str]] = {}
    try:
        for underlying, chains in underlying_items(raw):
            exchange = "BFO" if underlying in _BFO_UNDERLYINGS else "NFO"
            for expiry, strikes in chains.items():
                if expiry == "expiries" or not isinstance(strikes, dict):
                    continue
                for strike, info in strikes.items():
                    strike_value = safe_float(strike)
                    if strike_value is None or not isinstance(info, dict):
                        continue  # one malformed row must not drop the rest of the index
                    for option_type, infix in (("CE", "C"), ("PE", "P")):
                        prefix = option_type.lower()
                        native_tsym = str(info.get(f"{prefix}_tsym") or "").upper()
                        leg_token = info.get(f"{prefix}_token")
                        if not native_tsym or not leg_token:
                            continue
                        alias = _suffix_convention_alias(native_tsym, strike, infix, option_type)
                        names = [name for name in (native_tsym, alias) if name]
                        contract = {
                            "token": leg_token,
                            "lot_size": info.get("lot_size"),
                            "tick_size": info.get("tick_size") or DEFAULT_TICK_SIZE,
                            "exchange": exchange,
                            "underlying": underlying,
                            "expiry": expiry,
                            "strike": strike_value,
                            "option_type": option_type,
                        }
                        for name in names:
                            tsym_index.setdefault(name, (contract, names))
                        token_index.setdefault(str(leg_token), names)
    except Exception as exc:
        logger.error(f"[OptionMaster] Failed to build lookup indexes: {exc}", exc_info=True)
    return tsym_index, token_index


_raw: dict[str, dict] = _load(_FILE) or {}
_tsym_index, _token_index = _build_indexes(_raw)


def today_ist() -> date:
    return _today_ist()


def _lookup_key(value) -> str | None:
    """Normalized key for underlying/tsym lookups; None for non-text input,
    so every accessor below answers "not found" instead of raising."""
    if not isinstance(value, str):
        return None
    key = value.strip().upper()
    return key or None


def _chains_for(underlying) -> dict:
    chains = _raw.get(_lookup_key(underlying))
    return chains if isinstance(chains, dict) else {}


def is_valid_underlying(underlying: str) -> bool:
    return bool(_chains_for(underlying))


def get_expiries(underlying: str) -> list[str]:
    """All listed expiries (ISO 'YYYY-MM-DD', ascending) for an underlying."""
    expiries = _chains_for(underlying).get("expiries")
    return expiries if isinstance(expiries, list) else []


def upcoming_expiries(underlying: str, today: date | None = None, now: datetime | None = None) -> list[str]:
    """Expiries still tradable (IST), ascending - see
    ScripMasterRefresher.upcoming_expiry_dates for the expiry-day close rule."""
    return upcoming_expiry_dates(get_expiries(underlying), today=today, now=now)


def nearest_expiry(underlying: str, today: date | None = None, now: datetime | None = None) -> str | None:
    """Earliest still-tradable expiry, or None if none is left."""
    upcoming = upcoming_expiries(underlying, today=today, now=now)
    return upcoming[0] if upcoming else None


def get_strike_chain(underlying: str, expiry: str) -> dict[str, dict]:
    """strike (str) -> {ce_token, ce_tsym, pe_token, pe_tsym, lot_size, tick_size} for one (underlying, expiry)."""
    chain = _chains_for(underlying).get(expiry) if isinstance(expiry, str) and expiry != "expiries" else None
    return chain if isinstance(chain, dict) else {}


def find_by_tsym(tsym: str) -> dict | None:
    """
    Reverse lookup for order placement and position building: tradingsymbol
    -> {token, lot_size, tick_size, exchange, underlying, expiry, strike, option_type}.
    Accepts both Shoonya's native tradingsymbol convention
    ('NIFTY14JUL26C23950' - C/P immediately after the expiry date) and the
    CE/PE-suffix convention ('NIFTY14JUL2623950CE' - strike then CE/PE),
    since callers have historically sent either. Returns a copy, so callers
    may mutate it freely.
    """
    entry = _tsym_index.get(_lookup_key(tsym))
    return dict(entry[0]) if entry is not None else None


def find_tsym_aliases(tsym: str) -> list[str]:
    """
    Given a tradingsymbol in EITHER convention, returns every equivalent
    tradingsymbol string for the same contract (native and CE/PE-suffix -
    see find_by_tsym's docstring), native first. Used by
    StopOrderTriggerService callers that only have one order's own symbol
    string but must also check the other convention's resting orders for the
    same real contract, since order_book.symbol is matched by exact string
    equality and the matching engine keeps separate resting pools per
    convention string. Returns [] if tsym isn't recognized.
    """
    entry = _tsym_index.get(_lookup_key(tsym))
    return list(entry[1]) if entry is not None else []


def find_tsym_aliases_by_token(token: str) -> list[str]:
    """
    Reverse lookup for the live tick feed (service/positionTickService.py):
    Shoonya instrument token -> every tradingsymbol string a resting order
    for this contract could be stored under. Ticks arrive keyed by token, not
    symbol, but resting STOP/STOPLIMIT orders are keyed by whatever symbol
    string the client originally submitted - which may be in either the
    native ('NIFTY14JUL26C23950') or CE/PE-suffix ('NIFTY14JUL2623950CE')
    convention (see find_by_tsym's docstring). Returns both forms (native
    first) so callers can check order_book rows against either; returns []
    if the token isn't recognized.
    """
    if token is None or token == "":
        return []
    return list(_token_index.get(str(token), []))


def _install(raw: dict) -> None:
    """Builds the indexes for `raw` first, then swaps all three globals, so
    readers never see a half-built index."""
    global _raw, _tsym_index, _token_index
    tsym_index, token_index = _build_indexes(raw)
    _raw, _tsym_index, _token_index = raw, tsym_index, token_index


def reload() -> bool:
    """Re-reads master_options.json from disk without restarting the
    process. An unreadable file keeps the current in-memory master (never
    swaps in an empty one); returns whether a new master was installed."""
    raw = _load(_FILE)
    if raw is None:
        logger.error("[OptionMaster] reload: file unreadable - keeping the current in-memory master")
        return False
    _install(raw)
    return True


def is_stale(today: date | None = None) -> bool:
    """True when the in-memory master can't be trusted as today's contract
    list - see ScripMasterRefresher.is_master_stale."""
    return is_master_stale(_raw, today)


def _download_from_broker() -> dict:
    from scripts.build_option_master import download_option_master
    return download_option_master()


_refresher = ScripMasterRefresher(
    name="OptionMaster",
    file_path=_FILE,
    download=_download_from_broker,
    install=_install,
    current_master=lambda: _raw,
)


def refresh_from_broker() -> bool:
    """Blocking download + atomic write + reload. Never raises."""
    return _refresher.refresh_now()


async def schedule_daily_refresh(app=None) -> None:
    """
    Background task (started from app.py lifespan). Refreshes immediately at
    startup when the master is stale (previously the first refresh only
    happened 24h after boot, so a server restarted every day never refreshed
    at all, and the chain fell through to a far quarterly expiry), retries
    until it succeeds, then refreshes daily at 08:15 IST since new weekly
    expiries are listed and tokens occasionally get reshuffled.
    """
    await _refresher.run_forever()
