"""
Persistence for order_margin_blocks - the order-level record of margin
blocked/released for a single F&O order. Every method takes a caller-owned
cursor: margin block writes must always happen inside the same transaction
as the order/wallet/position mutation they accompany, never on their own
connection, so a mid-flight failure rolls everything back together.
"""

import logging

from utils.query_loader import QueryLoader

logger = logging.getLogger(__name__)


class MarginBlockPersistence:
    """Reads and writes order_margin_blocks rows."""

    def __init__(self):
        self.logger = logger

    def insert_block(self, cursor, block: dict) -> int:
        """
        Insert a new ACTIVE margin block for an order.

        Args:
            cursor: Active database cursor from the caller's transaction
            block: dict with keys order_id, user_id, tsym, exchange,
                contract_type, side, qty, lot_size, blocked_amount,
                premium_component, notional_component, reference_price,
                reference_source, reference_source_tier,
                price_source_multiplier_used, moneyness_multiplier_used,
                expiry_multiplier_used, notional_pct_or_span_pct

        Returns:
            The new order_margin_blocks.id

        Raises:
            ValueError: If parameters are invalid
            Exception: If the insert fails
        """
        if cursor is None:
            raise ValueError("Cursor cannot be None")
        if block is None:
            raise ValueError("Block payload cannot be None")

        try:
            cursor.execute(
                QueryLoader.get('margin.yaml', 'insert_order_margin_block'),
                (
                    block["order_id"], block["user_id"], block["tsym"], block["exchange"],
                    block["contract_type"], block["side"], block["qty"], block["lot_size"],
                    block["blocked_amount"], block.get("premium_component"),
                    block["notional_component"], block["reference_price"],
                    block["reference_source"], block["reference_source_tier"],
                    block.get("price_source_multiplier_used", 1.0),
                    block.get("moneyness_multiplier_used"),
                    block.get("expiry_multiplier_used", 1.0),
                    block["notional_pct_or_span_pct"],
                )
            )
            row = cursor.fetchone()
            if row is None:
                raise Exception("Insert of order_margin_blocks did not return an id")
            return row[0]
        except Exception as ex:
            self.logger.error(f"Error inserting order margin block: {str(ex)}")
            raise Exception(f"Error inserting order margin block: {str(ex)}") from ex

    def get_active_block_for_order(self, cursor, order_id: int) -> dict | None:
        """Fetch (and row-lock) the ACTIVE margin block for an order, if any."""
        if cursor is None:
            raise ValueError("Cursor cannot be None")
        if order_id is None or order_id <= 0:
            raise ValueError("Order ID must be a positive integer")

        try:
            cursor.execute(
                QueryLoader.get('margin.yaml', 'get_active_block_for_order'),
                (order_id,)
            )
            row = cursor.fetchone()
            if row is None:
                return None
            columns = [desc[0] for desc in cursor.description]
            return dict(zip(columns, row))
        except Exception as ex:
            self.logger.error(f"Error fetching active margin block for order {order_id}: {str(ex)}")
            raise Exception(f"Error fetching active margin block for order {order_id}: {str(ex)}") from ex

    def release_block(self, cursor, block_id: int, release_reason: str) -> None:
        """Marks a margin block RELEASED. Does not itself move any money -
        callers must separately recompute and persist the wallet/position
        blocked_margin totals."""
        if cursor is None:
            raise ValueError("Cursor cannot be None")
        if block_id is None or block_id <= 0:
            raise ValueError("Block ID must be a positive integer")
        if release_reason not in ("CANCEL", "FILL", "EXPIRY", "AMEND"):
            raise ValueError(f"Invalid release_reason: {release_reason}")

        try:
            cursor.execute(
                QueryLoader.get('margin.yaml', 'release_order_margin_block'),
                (release_reason, block_id)
            )
        except Exception as ex:
            self.logger.error(f"Error releasing margin block {block_id}: {str(ex)}")
            raise Exception(f"Error releasing margin block {block_id}: {str(ex)}") from ex
