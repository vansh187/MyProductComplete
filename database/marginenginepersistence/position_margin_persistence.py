"""
Persistence for the positions.blocked_margin / positions.contract_type
columns. Kept separate from database/positionPersistence.py because that
class owns the P&L/quantity fields written on open/close fills, while this
one owns the margin side, written by MarginEngine.reconcile_on_fill().
"""

import logging
from decimal import Decimal

from utils.query_loader import QueryLoader

logger = logging.getLogger(__name__)


class PositionMarginPersistence:
    """Reads/writes blocked_margin and contract_type on positions rows."""

    def __init__(self):
        self.logger = logger

    def get_position_for_margin(self, cursor, user_id: int, tsym: str) -> dict | None:
        """Locks and returns the margin-relevant fields of a position row."""
        if cursor is None:
            raise ValueError("Cursor cannot be None")
        if user_id is None or user_id <= 0:
            raise ValueError("User ID must be a positive integer")
        if not tsym or not tsym.strip():
            raise ValueError("tsym cannot be empty")

        try:
            cursor.execute(
                QueryLoader.get('margin.yaml', 'get_position_for_margin'),
                (user_id, tsym.strip().upper())
            )
            row = cursor.fetchone()
            if row is None:
                return None
            columns = ["user_id", "tsym", "netqty", "exchange", "token", "expiry",
                       "contract_type", "blocked_margin", "status"]
            result = dict(zip(columns, row))
            result["netqty"] = Decimal(str(result["netqty"]))
            result["blocked_margin"] = Decimal(str(result["blocked_margin"]))
            return result
        except Exception as ex:
            self.logger.error(f"Error locking position for margin, user {user_id} tsym {tsym}: {str(ex)}")
            raise Exception(f"Error locking position for margin, user {user_id} tsym {tsym}: {str(ex)}") from ex

    def update_blocked_margin(self, cursor, user_id: int, tsym: str, new_blocked_margin) -> None:
        """Sets positions.blocked_margin to an absolute new value for (user_id, tsym)."""
        if cursor is None:
            raise ValueError("Cursor cannot be None")
        if user_id is None or user_id <= 0:
            raise ValueError("User ID must be a positive integer")
        if not tsym or not tsym.strip():
            raise ValueError("tsym cannot be empty")
        if new_blocked_margin is None or new_blocked_margin < 0:
            raise ValueError("new_blocked_margin must be a non-negative number")

        try:
            cursor.execute(
                QueryLoader.get('margin.yaml', 'update_position_blocked_margin'),
                (new_blocked_margin, user_id, tsym.strip().upper())
            )
        except Exception as ex:
            self.logger.error(f"Error updating position blocked_margin, user {user_id} tsym {tsym}: {str(ex)}")
            raise Exception(f"Error updating position blocked_margin, user {user_id} tsym {tsym}: {str(ex)}") from ex

    def get_open_positions_expiring_by(self, cursor, expiry_date) -> list[dict]:
        """Used by the EOD sweep to find open F&O positions at/past expiry."""
        if cursor is None:
            raise ValueError("Cursor cannot be None")
        if expiry_date is None:
            raise ValueError("expiry_date cannot be None")

        try:
            cursor.execute(
                QueryLoader.get('margin.yaml', 'get_open_fno_positions_expiring_by'),
                (expiry_date,)
            )
            rows = cursor.fetchall()
            columns = ["user_id", "tsym", "exchange", "token", "expiry", "contract_type", "netqty", "blocked_margin"]
            return [dict(zip(columns, r)) for r in rows]
        except Exception as ex:
            self.logger.error(f"Error fetching expiring open F&O positions: {str(ex)}")
            raise Exception(f"Error fetching expiring open F&O positions: {str(ex)}") from ex

    def get_all_open_positions(self, cursor) -> list[dict]:
        """Used by the EOD flag-for-review sweep to recompute current MTM margin."""
        if cursor is None:
            raise ValueError("Cursor cannot be None")

        try:
            cursor.execute(QueryLoader.get('margin.yaml', 'get_all_open_fno_positions'))
            rows = cursor.fetchall()
            columns = ["user_id", "tsym", "exchange", "token", "expiry", "contract_type", "netqty", "netavgprc", "blocked_margin"]
            return [dict(zip(columns, r)) for r in rows]
        except Exception as ex:
            self.logger.error(f"Error fetching all open positions: {str(ex)}")
            raise Exception(f"Error fetching all open positions: {str(ex)}") from ex
