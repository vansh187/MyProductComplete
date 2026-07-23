"""
MasterAccountEntitlementPersistence - Ticket 12 groundwork (schema-only, no
live callers yet).

Once ticket 11 (live Shoonya order routing for the master account) is built
as its own project (see Part_B_Deferred_Tickets.md), this becomes the sole
source of truth for which internal user(s) - and what quantity each - a given
pooled master-account Shoonya order/position actually belongs to. Records an
entitlement row at internal-order-creation time (broker_order_id starts
NULL), then set_broker_order_id() links it once the real Shoonya order_id
comes back from the broker.

Not wired into any live code path today - there is nothing to link to until
that routing exists (confirmed via a full codebase survey: no code anywhere
calls Shoonya's place_order or an equivalent). Exists now purely so the data
model is ready.
"""

import logging
from typing import Optional, List, Dict, Any

import psycopg2.extras

from database.PostgresConnectionFactory import PostgresConnectionFactory
from utils.query_loader import QueryLoader

logger = logging.getLogger(__name__)


class MasterAccountEntitlementPersistence:

    @staticmethod
    def create_entitlement(internal_order_id: int, user_id: int, symbol: str,
                            side: str, quantity: int, cursor,
                            broker_order_id: Optional[str] = None) -> int:
        """Records an entitlement row within the caller's own transaction
        (mirrors get_order_snapshot's caller-owned-cursor pattern) - meant to
        be called in the same transaction as the internal order itself."""
        if internal_order_id is None or internal_order_id <= 0:
            raise ValueError("internal_order_id must be a positive integer")
        if user_id is None or user_id <= 0:
            raise ValueError("user_id must be a positive integer")
        if not symbol or not symbol.strip():
            raise ValueError("symbol cannot be empty")
        if not side or side not in ("BUY", "SELL"):
            raise ValueError("side must be BUY or SELL")
        if quantity is None or quantity <= 0:
            raise ValueError("quantity must be a positive integer")

        cursor.execute(
            QueryLoader.get('master_account_entitlements.yaml', 'insert_entitlement'),
            (broker_order_id, internal_order_id, user_id, symbol, side, quantity)
        )
        row = cursor.fetchone()
        if row is None:
            raise Exception("Failed to create entitlement row - no ID returned")
        return row[0] if not isinstance(row, dict) else row["id"]

    @staticmethod
    def set_broker_order_id(entitlement_id: int, broker_order_id: str, cursor) -> None:
        if entitlement_id is None or entitlement_id <= 0:
            raise ValueError("entitlement_id must be a positive integer")
        if not broker_order_id or not broker_order_id.strip():
            raise ValueError("broker_order_id cannot be empty")

        cursor.execute(
            QueryLoader.get('master_account_entitlements.yaml', 'set_broker_order_id'),
            (broker_order_id, entitlement_id)
        )

    @staticmethod
    def get_entitlements_for_broker_order(broker_order_id: str) -> List[Dict[str, Any]]:
        if not broker_order_id or not broker_order_id.strip():
            raise ValueError("broker_order_id cannot be empty")

        conn = None
        cursor = None
        try:
            conn = PostgresConnectionFactory.create_connection()
            cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cursor.execute(
                QueryLoader.get('master_account_entitlements.yaml', 'get_entitlements_for_broker_order'),
                (broker_order_id,)
            )
            return cursor.fetchall() or []
        finally:
            if cursor is not None:
                cursor.close()
            if conn is not None:
                conn.close()

    @staticmethod
    def get_entitlement_for_internal_order(internal_order_id: int) -> Optional[Dict[str, Any]]:
        if internal_order_id is None or internal_order_id <= 0:
            raise ValueError("internal_order_id must be a positive integer")

        conn = None
        cursor = None
        try:
            conn = PostgresConnectionFactory.create_connection()
            cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cursor.execute(
                QueryLoader.get('master_account_entitlements.yaml', 'get_entitlement_for_internal_order'),
                (internal_order_id,)
            )
            return cursor.fetchone()
        finally:
            if cursor is not None:
                cursor.close()
            if conn is not None:
                conn.close()
