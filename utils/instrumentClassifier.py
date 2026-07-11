"""
Single source of truth for "is this instrument F&O?" - kept dependency-free
(no DB/service imports) so any layer (execution, dashboard, reporting) can
reuse the same rule without pulling in unrelated concerns.
"""

import re

FO_EXCHANGES = {"NFO", "BFO"}

# Real Shoonya futures trading-symbol conventions differ by exchange -
# confirmed from a live scrip master: NFO index futures end in a single 'F'
# (e.g. 'NIFTY28JUL26F'), while BFO Sensex futures end in 'FUT'
# (e.g. 'SENSEX5026AUGFUT'). Only used as a fallback signal now that
# appconfig/FutureMaster.py provides an authoritative lookup - kept broad
# enough to catch either convention when the master is stale/missing.
_NFO_FUTURES_PATTERN = re.compile(r"^[A-Z]+\d{2}[A-Z]{3}\d{2}F$")


def is_fo_exchange(exchange: str | None) -> bool:
    """True if this exchange trades F&O contracts (options today; futures once supported)."""
    return bool(exchange) and exchange.upper() in FO_EXCHANGES


def looks_like_future_symbol(tsym: str | None) -> bool:
    """Best-effort futures detection by trading-symbol convention, used only
    as a fallback when appconfig/FutureMaster.py doesn't recognize the
    symbol (master stale/missing) - see
    service/marginengine/margin_engine.py.resolve_contract_type()."""
    if not tsym:
        return False
    t = tsym.strip().upper()
    return t.endswith("FUT") or bool(_NFO_FUTURES_PATTERN.match(t))
