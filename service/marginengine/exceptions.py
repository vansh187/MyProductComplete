"""Exception types raised by the margin engine. All are subclasses of
MarginEngineError so callers (api/orders.py) can catch one type and map it
to the right HTTP error code, per FnO_Margin_Engine_Design.md section 10."""


class MarginEngineError(Exception):
    """Base class for every margin-engine-raised error."""


class InsufficientMarginError(MarginEngineError):
    """Raised when available balance cannot cover the required margin."""

    def __init__(self, required_margin, available_balance, shortfall):
        self.required_margin = required_margin
        self.available_balance = available_balance
        self.shortfall = shortfall
        super().__init__(
            f"Insufficient margin: required={required_margin}, "
            f"available={available_balance}, shortfall={shortfall}"
        )


class ReferencePriceUnresolvedError(MarginEngineError):
    """Raised when every price-resolution tier fails for an instrument."""

    def __init__(self, tsym, attempted_sources):
        self.tsym = tsym
        self.attempted_sources = attempted_sources
        super().__init__(f"Could not resolve a reliable reference price for {tsym}")


class LockOrderViolationError(MarginEngineError):
    """Raised (debug/test builds only) when a lock is acquired out of the
    mandated position-lock-before-wallet-lock order."""
