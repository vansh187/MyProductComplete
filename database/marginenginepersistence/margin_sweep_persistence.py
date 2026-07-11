"""Persistence for the EOD sweep's own output tables: peak_margin_snapshot
(daily record of blocked margin/available balance per user) and
margin_review_flags (accounts whose recomputed MTM margin would exceed
available balance - flagged for manual ops review, never auto-liquidated)."""

import logging

from utils.query_loader import QueryLoader

logger = logging.getLogger(__name__)

VALID_FLAG_REASONS = ("MTM_BREACH", "STALE_PRICE", "MANUAL")


class MarginSweepPersistence:
    """Writes peak_margin_snapshot and margin_review_flags rows for the EOD sweep."""

    def __init__(self):
        self.logger = logger

    def insert_snapshot(self, cursor, user_id: int, snapshot_date,
                         total_blocked_margin, available_balance) -> None:
        if cursor is None:
            raise ValueError("Cursor cannot be None")
        if user_id is None or user_id <= 0:
            raise ValueError("User ID must be a positive integer")
        if snapshot_date is None:
            raise ValueError("snapshot_date cannot be None")

        try:
            cursor.execute(
                QueryLoader.get('margin.yaml', 'insert_peak_margin_snapshot'),
                (user_id, snapshot_date, total_blocked_margin, available_balance)
            )
        except Exception as ex:
            self.logger.error(f"Error inserting peak margin snapshot for user {user_id}: {str(ex)}")
            raise Exception(f"Error inserting peak margin snapshot for user {user_id}: {str(ex)}") from ex

    def insert_review_flag(self, cursor, user_id: int, flag_date, flag_reason: str,
                            required_margin_recomputed, available_balance_at_flag) -> None:
        if cursor is None:
            raise ValueError("Cursor cannot be None")
        if user_id is None or user_id <= 0:
            raise ValueError("User ID must be a positive integer")
        if flag_date is None:
            raise ValueError("flag_date cannot be None")
        if flag_reason not in VALID_FLAG_REASONS:
            raise ValueError(f"Invalid flag_reason: {flag_reason}")

        try:
            cursor.execute(
                QueryLoader.get('margin.yaml', 'insert_margin_review_flag'),
                (user_id, flag_date, flag_reason, required_margin_recomputed, available_balance_at_flag)
            )
        except Exception as ex:
            self.logger.error(f"Error inserting margin review flag for user {user_id}: {str(ex)}")
            raise Exception(f"Error inserting margin review flag for user {user_id}: {str(ex)}") from ex
