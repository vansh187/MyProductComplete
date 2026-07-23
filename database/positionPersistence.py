"""
Position Persistence Layer - Database operations for the positions table.
"""

import logging
from typing import Optional, Dict, Any

from utils.query_loader import QueryLoader

logger = logging.getLogger(__name__)


class PositionPersistence:
    """Position persistence layer for database operations."""

    def __init__(self):
        self.logger = logger

    def get_position(self, user_id: int, tsym: str, cursor) -> Optional[Dict[str, Any]]:
        """
        Fetch (and row-lock) the current position for a user+instrument, if one exists.

        Args:
            user_id: User ID
            tsym: Trading symbol (position table's key alongside user_id)
            cursor: Database cursor (shared with the calling transaction)

        Returns:
            Position dictionary or None if no row exists yet

        Raises:
            ValueError: If parameters are invalid
        """
        if user_id is None or user_id <= 0:
            self.logger.error(f"get_position() received invalid user_id: {user_id}")
            raise ValueError("User ID must be a positive integer")

        if not tsym or not tsym.strip():
            self.logger.error("get_position() received empty tsym")
            raise ValueError("tsym cannot be empty")

        if cursor is None:
            self.logger.error("get_position() received None cursor")
            raise ValueError("Database cursor cannot be None")

        cursor.execute(
            QueryLoader.get('positions.yaml', 'select_position_for_update'),
            (user_id, tsym)
        )
        return cursor.fetchone()

    def upsert_position(self, position: Dict[str, Any], cursor) -> None:
        """
        Insert a new position row, or update the existing one for (user_id, tsym).

        Args:
            position: Dict with all position fields (see queries/positions.yaml)
            cursor: Database cursor (shared with the calling transaction)

        Raises:
            ValueError: If parameters are invalid
        """
        if position is None:
            self.logger.error("upsert_position() received None position")
            raise ValueError("Position data cannot be None")

        if position.get("user_id") is None or position.get("user_id") <= 0:
            self.logger.error(f"upsert_position() received invalid user_id: {position.get('user_id')}")
            raise ValueError("User ID must be a positive integer")

        if not position.get("tsym") or not str(position.get("tsym")).strip():
            self.logger.error("upsert_position() received empty tsym")
            raise ValueError("tsym cannot be empty")

        if cursor is None:
            self.logger.error("upsert_position() received None cursor")
            raise ValueError("Database cursor cannot be None")

        cursor.execute(
            QueryLoader.get('positions.yaml', 'upsert_position'),
            (
                position["user_id"],
                position["tsym"],
                position.get("broker"),
                position.get("token"),
                position.get("exchange"),
                position.get("underlying"),
                position.get("expiry"),
                position.get("strike"),
                position.get("option_type"),
                position.get("lot_size"),
                position.get("product_type"),
                position.get("source"),
                position.get("netqty", 0),
                position.get("netavgprc", 0),
                position.get("buyqty", 0),
                position.get("sellqty", 0),
                position.get("buyavgprc", 0),
                position.get("sellavgprc", 0),
                position.get("realized_pnl", 0),
                position.get("status", "OPEN"),
            )
        )
        self.logger.info(
            f"Position upserted: user_id={position['user_id']}, tsym={position['tsym']}, "
            f"netqty={position.get('netqty', 0)}, status={position.get('status', 'OPEN')}"
        )
