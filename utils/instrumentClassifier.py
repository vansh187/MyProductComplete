"""
Single source of truth for "is this instrument F&O?" - kept dependency-free
(no DB/service imports) so any layer (execution, dashboard, reporting) can
reuse the same rule without pulling in unrelated concerns.
"""

FO_EXCHANGES = {"NFO", "BFO"}


def is_fo_exchange(exchange: str | None) -> bool:
    """True if this exchange trades F&O contracts (options today; futures once supported)."""
    return bool(exchange) and exchange.upper() in FO_EXCHANGES
