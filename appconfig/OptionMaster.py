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


# Sensex options trade on Shoonya's separate BFO segment; every other tracked
# underlying (NIFTY, BANKNIFTY, FINNIFTY) is NFO. Mirrors BFO_UNDERLYINGS in
# scripts/build_option_master.py.
_BFO_UNDERLYINGS = {"SENSEX"}


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


def find_by_tsym(tsym: str) -> dict | None:
    """
    Reverse lookup for order placement and position building: tradingsymbol
    -> {token, lot_size, exchange, underlying, expiry, strike, option_type}.
    Accepts both Shoonya's native tradingsymbol convention
    ('NIFTY14JUL26C23950' - C/P immediately after the expiry date) and the
    CE/PE-suffix convention ('NIFTY14JUL2623950CE' - strike then CE/PE),
    since callers have historically sent either. Tradingsymbols are unique
    per contract, so this scans every underlying/expiry/strike without
    needing to parse the symbol string.
    """
    if not tsym:
        return None
    tsym = tsym.upper().strip()
    for underlying, chains in _raw.items():
        for expiry, strikes in chains.items():
            if expiry == "expiries":
                continue
            for strike, info in strikes.items():
                ce_tsym = info.get("ce_tsym", "").upper()
                pe_tsym = info.get("pe_tsym", "").upper()
                if tsym == ce_tsym or tsym == _suffix_convention_alias(ce_tsym, strike, "C", "CE"):
                    leg_token, option_type = info["ce_token"], "CE"
                elif tsym == pe_tsym or tsym == _suffix_convention_alias(pe_tsym, strike, "P", "PE"):
                    leg_token, option_type = info["pe_token"], "PE"
                else:
                    continue
                return {
                    "token": leg_token,
                    "lot_size": info["lot_size"],
                    "exchange": "BFO" if underlying in _BFO_UNDERLYINGS else "NFO",
                    "underlying": underlying,
                    "expiry": expiry,
                    "strike": float(strike),
                    "option_type": option_type,
                }
    return None


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
