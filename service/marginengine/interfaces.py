"""
Ports & adapters interfaces for the margin engine. Every external dependency
(formula, price source, persistence, real-broker margin API, position
lookup) is defined here as an abstract base class first. MarginEngine only
ever depends on these types, never on a concrete implementation, so swapping
an adapter later (e.g. a real broker SPAN API) never requires touching a
call site outside service/marginengine/.

All methods are ordinary instance methods (never @staticmethod/@classmethod)
so every implementation is a real object participating in normal OOP
polymorphism/dependency injection.
"""

from abc import ABC, abstractmethod
from typing import Optional

from service.marginengine.models import MarginContext, MarginResult, PriceResolution


class MarginCalculator(ABC):
    """Prices one order/position for one instrument class (OPTION or FUTURES)."""

    @abstractmethod
    def calculate(self, context: MarginContext) -> MarginResult:
        """Returns the full margin breakdown for this context.

        Raises:
            service.marginengine.exceptions.MarginEngineError: on any
                calculation failure (never lets a raw exception escape).
        """
        raise NotImplementedError


class ReferencePriceResolver(ABC):
    """Resolves a trustworthy reference price for an instrument, tiered by confidence."""

    @abstractmethod
    def resolve(self, tsym: str, contract_type: str, underlying: Optional[str],
                exchange: str, token: Optional[str],
                order_price=None) -> PriceResolution:
        """Returns the best available PriceResolution.

        Raises:
            service.marginengine.exceptions.ReferencePriceUnresolvedError: if
                every tier fails (Tier 5, terminal).
        """
        raise NotImplementedError


class MarginLedgerRepository(ABC):
    """Persists margin blocks/releases across order_margin_blocks, wallets,
    positions and margin_block_audit, inside a caller-owned transaction."""

    @abstractmethod
    def block_order_margin(self, cursor, user_id: int, order_id: int,
                            block_fields: dict, margin_result: MarginResult) -> int:
        raise NotImplementedError

    @abstractmethod
    def release_order_margin(self, cursor, order_id: int, release_reason: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def reconcile_position_margin(self, cursor, user_id: int, tsym: str,
                                   new_required_margin, contract_type: str) -> None:
        raise NotImplementedError


class BrokerMarginProvider(ABC):
    """Seam for a real broker SPAN/margin API. Today's only implementation
    (NullBrokerMarginProvider) always defers to the internal calculator;
    swapping in a real broker call later touches only this one adapter."""

    @abstractmethod
    def get_broker_margin(self, context: MarginContext) -> Optional[MarginResult]:
        """Returns a broker-computed MarginResult, or None to defer to the
        internal MarginCalculator (the only behavior implemented today)."""
        raise NotImplementedError


class PositionReader(ABC):
    """Read-only view into a user's current net position, used to detect
    closing trades (which release margin) vs. opening trades (which block it)."""

    @abstractmethod
    def get_net_position(self, user_id: int, tsym: str, cursor=None) -> Optional[dict]:
        raise NotImplementedError
