import re
from datetime import date
from decimal import Decimal, InvalidOperation

_MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}

# e.g. NIFTY07JUL2623800CE -> underlying=NIFTY, day=07, mon=JUL, yy=26, strike=23800, type=CE
_OPTION_PATTERN = re.compile(r"^([A-Z]+)(\d{2})([A-Z]{3})(\d{2})(\d+(?:\.\d+)?)(CE|PE)$")
# e.g. NIFTY28AUG25FUT -> underlying=NIFTY, day=28, mon=AUG, yy=25
_FUTURE_PATTERN = re.compile(r"^([A-Z]+)(\d{2})([A-Z]{3})(\d{2})FUT$")


def _build_expiry(day: str, mon: str, yy: str) -> date | None:
    month = _MONTHS.get(mon)
    if month is None:
        return None
    try:
        return date(2000 + int(yy), month, int(day))
    except ValueError:
        return None


def parse_fno_symbol(tsym: str) -> dict:
    """
    Parses an F&O trading symbol (e.g. NIFTY07JUL2623800CE, NIFTY28AUG25FUT)
    into its contract components. Falls back to Nones for anything that
    can't be derived from the symbol string alone (e.g. lot_size, token) -
    callers should treat a fully-None result as "unrecognized format",
    not an error, since order symbols are client-supplied and not validated
    against a scrip master.

    Returns:
        dict with keys: underlying, expiry (date|None), strike (Decimal|None),
        option_type ("CE"|"PE"|None), contract_type ("OPTION"|"FUTURES")
    """
    symbol = (tsym or "").upper().strip()

    match = _OPTION_PATTERN.match(symbol)
    if match:
        underlying, day, mon, yy, strike_str, option_type = match.groups()
        try:
            strike = Decimal(strike_str)
        except InvalidOperation:
            strike = None
        return {
            "underlying": underlying,
            "expiry": _build_expiry(day, mon, yy),
            "strike": strike,
            "option_type": option_type,
            "contract_type": "OPTION",
        }

    match = _FUTURE_PATTERN.match(symbol)
    if match:
        underlying, day, mon, yy = match.groups()
        return {
            "underlying": underlying,
            "expiry": _build_expiry(day, mon, yy),
            "strike": None,
            "option_type": None,
            "contract_type": "FUTURES",
        }

    # Unrecognized format - still need a valid contract_type (NOT NULL,
    # CHECK constraint only allows OPTION/FUTURES), default to OPTION.
    return {
        "underlying": None,
        "expiry": None,
        "strike": None,
        "option_type": None,
        "contract_type": "OPTION",
    }
