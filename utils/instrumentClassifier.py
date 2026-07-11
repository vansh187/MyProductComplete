"""
Single source of truth for "is this instrument F&O?" - kept dependency-free
(no DB/service imports) so any layer (execution, dashboard, reporting) can
reuse the same rule without pulling in unrelated concerns.
"""

FO_EXCHANGES = {"NFO", "BFO"}


def is_fo_exchange(exchange: str | None) -> bool:
    """True if this exchange trades F&O contracts (options today; futures once supported)."""
    return bool(exchange) and exchange.upper() in FO_EXCHANGES


def looks_like_future_symbol(tsym: str | None) -> bool:
    """Best-effort futures detection by trading-symbol convention (e.g.
    'NIFTY28AUG25FUT'). This codebase has no futures instrument master yet
    (appconfig/OptionMaster.py only indexes options) - this heuristic is the
    placeholder service/marginengine/margin_engine.py uses to classify a
    futures order until a real futures scrip master exists."""
    return bool(tsym) and tsym.strip().upper().endswith("FUT")
