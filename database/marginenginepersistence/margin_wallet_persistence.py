"""
Persistence for the wallets.blocked_margin column - kept separate from
database/walletbalancepersistence/WalletBalancePersistence.py because that
class owns cash balance movement (credits/debits on settlement), while this
one owns the margin-block side of the same row. Both lock the same row via
`FOR UPDATE`; callers must respect the documented lock-ordering rule
(position lock before wallet lock) to avoid deadlocking against
PositionService/PositionCache.
"""

import logging
from decimal import Decimal

from utils.query_loader import QueryLoader

logger = logging.getLogger(__name__)


class MarginWalletPersistence:
    """Reads/writes the blocked_margin column on wallets, row-locked."""

    def __init__(self):
        self.logger = logger

    def get_wallet_for_update(self, cursor, user_id: int) -> dict | None:
        """Locks and returns {user_id, balance, blocked_margin} for a wallet."""
        if cursor is None:
            raise ValueError("Cursor cannot be None")
        if user_id is None or user_id <= 0:
            raise ValueError("User ID must be a positive integer")

        try:
            cursor.execute(
                QueryLoader.get('margin.yaml', 'get_wallet_for_margin_update'),
                (user_id,)
            )
            row = cursor.fetchone()
            if row is None:
                return None
            return {"user_id": row[0], "balance": Decimal(str(row[1])), "blocked_margin": Decimal(str(row[2]))}
        except Exception as ex:
            self.logger.error(f"Error locking wallet for margin update, user {user_id}: {str(ex)}")
            raise Exception(f"Error locking wallet for margin update, user {user_id}: {str(ex)}") from ex

    def update_blocked_margin(self, cursor, user_id: int, new_blocked_margin) -> None:
        """Sets wallets.blocked_margin to an absolute new value (never a delta -
        callers must have already computed the post-block/post-release total)."""
        if cursor is None:
            raise ValueError("Cursor cannot be None")
        if user_id is None or user_id <= 0:
            raise ValueError("User ID must be a positive integer")
        if new_blocked_margin is None or new_blocked_margin < 0:
            raise ValueError("new_blocked_margin must be a non-negative number")

        try:
            cursor.execute(
                QueryLoader.get('margin.yaml', 'update_wallet_blocked_margin'),
                (new_blocked_margin, user_id)
            )
        except Exception as ex:
            self.logger.error(f"Error updating blocked_margin for user {user_id}: {str(ex)}")
            raise Exception(f"Error updating blocked_margin for user {user_id}: {str(ex)}") from ex

    def get_all_wallet_balances(self, cursor) -> list[dict]:
        """Used by the EOD sweep: every wallet's balance + blocked_margin."""
        if cursor is None:
            raise ValueError("Cursor cannot be None")

        try:
            cursor.execute(QueryLoader.get('margin.yaml', 'get_all_wallet_balances'))
            rows = cursor.fetchall()
            return [{"user_id": r[0], "balance": Decimal(str(r[1])), "blocked_margin": Decimal(str(r[2]))} for r in rows]
        except Exception as ex:
            self.logger.error(f"Error fetching all wallet balances: {str(ex)}")
            raise Exception(f"Error fetching all wallet balances: {str(ex)}") from ex
