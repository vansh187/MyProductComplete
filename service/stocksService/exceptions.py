class MarketDataUnavailableError(Exception):
    """Raised when neither the tick cache nor a bounded REST fallback could
    produce data for an otherwise-valid, symbol-master-resolved instrument -
    e.g. Shoonya itself is disconnected. The API layer maps this to HTTP 503,
    never lets it surface as an unhandled 500."""
