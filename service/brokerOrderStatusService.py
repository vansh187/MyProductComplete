"""
BrokerOrderStatusService - the broker's own view of one live order, for
GET /orders/by-client-id/{id}.

Looks the order up by its norenordno (SingleOrdHist, latest entry first)
when we have it. Otherwise - placement status uncertain, or the
broker_order_id write lost - finds it in the master account's order book:
`remarks` (stamped with the client_order_id at placement) AND contract/side/
quantity must match, and the broker order must not already belong to a
different internal order. The master book holds every user's orders and
remarks is free text the broker doesn't validate, so a remarks match alone
is never trusted (even though our own orders table keeps client_order_id
globally unique via orders_client_order_id_key). A match is saved as the order's
broker_order_id, so every later poll uses the cheap single-order history
instead of the whole book.

Only orders actually sent to the broker (source = BROKER_ROUTED_SOURCE) are
ever looked up at the broker.

Deliberately instance-based (no static/class methods or class-level state).
"""

import asyncio
import logging
import time
from typing import Any, Dict, Optional

from api.models import BROKER_ROUTED_SOURCE
from database.orderPersistence import OrderPersistence
from service.brokerOrderMatcher import BrokerOrderMatcher
from utils.safe_numbers import safe_float, safe_int

logger = logging.getLogger(__name__)

BROKER_STATUS_TIMEOUT_SECS = 5.0
# Concurrent/rapid polls share one order-book download instead of each
# pulling the whole master book.
ORDER_BOOK_CACHE_SECS = 1.0


class BrokerOrderStatusService:

    def __init__(self, timeout_secs: float = BROKER_STATUS_TIMEOUT_SECS,
                 order_book_cache_secs: float = ORDER_BOOK_CACHE_SECS,
                 order_persistence: Optional[OrderPersistence] = None,
                 matcher: Optional[BrokerOrderMatcher] = None):
        self._timeout_secs = timeout_secs
        self._order_book_cache_secs = order_book_cache_secs
        self._order_persistence = order_persistence or OrderPersistence()
        self._matcher = matcher or BrokerOrderMatcher()
        self._order_book_lock = asyncio.Lock()
        self._order_book: Optional[list] = None
        self._order_book_at = 0.0

    def _normalize(self, entry: Dict[str, Any]) -> Dict[str, Any]:
        filled_qty = safe_int(entry.get("fillshares"), 0)
        avg_fill_price = safe_float(entry.get("avgprc")) if filled_qty > 0 else None
        return {
            "broker_order_id": entry.get("norenordno"),
            "status": entry.get("status"),
            "filled_qty": filled_qty,
            "avg_fill_price": avg_fill_price,
            "rejreason": entry.get("rejreason") or None,
            "quantity": safe_int(entry.get("qty"), 0),
            "broker_time": entry.get("norentm"),
        }

    async def _call(self, func, *args):
        loop = asyncio.get_running_loop()
        return await asyncio.wait_for(loop.run_in_executor(None, func, *args), timeout=self._timeout_secs)

    async def _order_book_snapshot(self, shoonya, fetched_after: Optional[float] = None) -> tuple[Optional[list], float]:
        """(master order book, monotonic time it was fetched). Reuses the
        shared copy while it is under ORDER_BOOK_CACHE_SECS old - or, when
        fetched_after is given, only if it was fetched after that moment.
        Concurrent callers wait on one download instead of each pulling the
        whole book."""
        def reusable() -> bool:
            if self._order_book is None:
                return False
            if fetched_after is not None:
                return self._order_book_at >= fetched_after
            return time.perf_counter() - self._order_book_at < self._order_book_cache_secs

        if reusable():
            return self._order_book, self._order_book_at
        async with self._order_book_lock:
            if reusable():
                return self._order_book, self._order_book_at
            book = await self._call(shoonya.get_order_book)
            fetched_at = time.perf_counter()
            if book is not None:
                self._order_book, self._order_book_at = book, fetched_at
            return book, fetched_at

    async def _linked_to_other_order(self, broker_order_id: str, order_id: int) -> bool:
        """True when this broker order is already some other internal order's
        (or the check itself failed - never claim an order we can't verify)."""
        try:
            other = await self._call(self._order_persistence.get_order_by_broker_order_id, broker_order_id)
            return other is not None and other.get("id") != order_id
        except Exception as exc:
            logger.warning(f"[BrokerOrderStatus] linkage check failed for {broker_order_id}: {exc}")
            return True

    async def _save_link(self, order_id: int, broker_order_id: str) -> None:
        try:
            await self._call(self._order_persistence.set_broker_order_id, order_id, broker_order_id)
            logger.info(f"[BrokerOrderStatus] Linked order_id={order_id} to broker_order_id={broker_order_id} from the order book")
        except Exception as exc:
            logger.warning(f"[BrokerOrderStatus] could not save broker_order_id={broker_order_id} for order {order_id}: {exc}")

    async def _owned_entries(self, order_book: list, order_row: Dict[str, Any]) -> list:
        """Order-book rows that are this order's broker side: remarks equal
        to its client_order_id, same contract/side/quantity, and not already
        linked to a different internal order."""
        client_order_id = str(order_row.get("client_order_id") or "")
        owned = []
        for entry in order_book:
            if not isinstance(entry, dict):
                continue
            if str(entry.get("remarks") or "").strip() != client_order_id or not self._matcher.matches(order_row, entry):
                continue
            broker_order_id = str(entry.get("norenordno") or "")
            if broker_order_id and not await self._linked_to_other_order(broker_order_id, order_row.get("id")):
                owned.append(entry)
        return owned

    async def _find_in_order_book(self, shoonya, order_row: Dict[str, Any]) -> tuple[Optional[Dict[str, Any]], Optional[str]]:
        requested_at = time.perf_counter()
        order_book, fetched_at = await self._order_book_snapshot(shoonya)
        if order_book is None:
            return None, "broker_unavailable"
        owned = await self._owned_entries(order_book, order_row)
        if not owned and fetched_at < requested_at:
            # A shared copy fetched before this request can predate the
            # order's arrival at the broker - "not found" is only ever
            # concluded from a book fetched after the request started.
            order_book, _ = await self._order_book_snapshot(shoonya, fetched_after=requested_at)
            if order_book is None:
                return None, "broker_unavailable"
            owned = await self._owned_entries(order_book, order_row)

        if not owned:
            return None, "not_found_at_broker"
        if len(owned) > 1:
            logger.error(f"[BrokerOrderStatus] {len(owned)} broker orders match order {order_row.get('id')} - not choosing")
            return None, "ambiguous_at_broker"

        entry = owned[0]
        await self._save_link(order_row.get("id"), str(entry.get("norenordno")))
        return self._normalize(entry), None

    async def get_status(self, shoonya, order_row: Dict[str, Any]) -> tuple[Optional[Dict[str, Any]], Optional[str]]:
        """
        Returns (status, None) when found, or (None, reason):
          not_routed_to_broker  - never sent to the broker (no broker call made)
          not_found_at_broker   - the broker does not show it (in a fresh book); this
                                  alone does NOT prove it never reached the exchange
          ambiguous_at_broker   - several broker orders fit; none is guessed
          shoonya_disconnected / broker_unavailable / broker_timeout / broker_status_failed
        Never raises.
        """
        try:
            if order_row.get("source") != BROKER_ROUTED_SOURCE and not order_row.get("broker_order_id"):
                return None, "not_routed_to_broker"
            if shoonya is None or not shoonya.is_connected:
                return None, "shoonya_disconnected"

            broker_order_id = order_row.get("broker_order_id")
            if broker_order_id:
                history = await self._call(shoonya.get_order_history, broker_order_id)
                if history is None:
                    return None, "broker_unavailable"
                for entry in history:
                    if isinstance(entry, dict):
                        return self._normalize(entry), None
                return None, "not_found_at_broker"

            return await self._find_in_order_book(shoonya, order_row)
        except asyncio.TimeoutError:
            return None, "broker_timeout"
        except Exception as exc:
            logger.error(f"[BrokerOrderStatus] lookup failed for order {order_row.get('id')}: {exc}", exc_info=True)
            return None, "broker_status_failed"
