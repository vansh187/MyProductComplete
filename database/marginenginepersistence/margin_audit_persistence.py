"""Persistence for margin_block_audit - an append-only trail of every
block/release/reconcile/flag event, for reconciliation and support debugging."""

import logging

from utils.query_loader import QueryLoader

logger = logging.getLogger(__name__)

VALID_EVENT_TYPES = ("BLOCK", "RELEASE", "RECONCILE", "FLAG")


class MarginAuditPersistence:
    """Writes append-only rows to margin_block_audit."""

    def __init__(self):
        self.logger = logger

    def insert_audit(self, cursor, order_margin_block_id, user_id: int,
                      event_type: str, amount_delta, reason_code: str) -> None:
        if cursor is None:
            raise ValueError("Cursor cannot be None")
        if user_id is None or user_id <= 0:
            raise ValueError("User ID must be a positive integer")
        if event_type not in VALID_EVENT_TYPES:
            raise ValueError(f"Invalid event_type: {event_type}")
        if amount_delta is None:
            raise ValueError("amount_delta cannot be None")
        if not reason_code or not reason_code.strip():
            raise ValueError("reason_code cannot be empty")

        try:
            cursor.execute(
                QueryLoader.get('margin.yaml', 'insert_margin_audit'),
                (order_margin_block_id, user_id, event_type, amount_delta, reason_code)
            )
        except Exception as ex:
            self.logger.error(f"Error inserting margin audit row for user {user_id}: {str(ex)}")
            raise Exception(f"Error inserting margin audit row for user {user_id}: {str(ex)}") from ex
