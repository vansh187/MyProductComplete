"""
Trade Settlement Service - Routes one side of a fill to the right ledger
(holdings for cash equity, positions for F&O) based on instrument type, so
callers (e.g. the execution engine) don't need to know that distinction exists.
"""

import logging
from typing import Dict, Any

from service.portfolioService import portfolioService
from service.positionService import PositionService
from utils.instrumentClassifier import is_fo_exchange

logger = logging.getLogger(__name__)

VALID_SIDES = ("BUY", "SELL")


class TradeSettlementService:
    """Settles one side of a matched fill into holdings or positions."""

    def __init__(self):
        self.portfolio_service = portfolioService()
        self.position_service = PositionService()
        self.logger = logger

    def settle_fill(self, user_id: int, side: str, order_snapshot: Dict[str, Any],
                     quantity: int, price, cursor) -> None:
        """
        Settle one fill for one side of a match.

        Args:
            user_id: User ID for this side of the trade
            side: 'BUY' or 'SELL'
            order_snapshot: dict from OrderService.get_order_snapshot() - must
                contain at least 'symbol' and 'exchange'
            quantity: Fill quantity
            price: Fill execution price
            cursor: Database cursor (shared with the calling transaction)

        Raises:
            ValueError: If parameters are invalid
        """
        if user_id is None or user_id <= 0:
            self.logger.error(f"settle_fill() received invalid user_id: {user_id}")
            raise ValueError("User ID must be a positive integer")

        if side not in VALID_SIDES:
            self.logger.error(f"settle_fill() received invalid side: {side}")
            raise ValueError(f"Side must be one of {VALID_SIDES}")

        if order_snapshot is None:
            self.logger.error("settle_fill() received None order_snapshot")
            raise ValueError("Order snapshot cannot be None")

        symbol = order_snapshot.get("symbol")
        if not symbol or not symbol.strip():
            self.logger.error("settle_fill() order_snapshot missing symbol")
            raise ValueError("Order snapshot must contain a symbol")

        if cursor is None:
            self.logger.error("settle_fill() received None cursor")
            raise ValueError("Database cursor cannot be None")

        if is_fo_exchange(order_snapshot.get("exchange")):
            self.position_service.apply_fill(user_id, side, order_snapshot, quantity, price, cursor)
        elif side == "BUY":
            self.portfolio_service.process_buyer(user_id, symbol, quantity, price, cursor)
        else:
            self.portfolio_service.process_seller(user_id, symbol, quantity, price, cursor)
