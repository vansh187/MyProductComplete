"""
LiveOrderRoutingService - places a real order on the Shoonya master account
for an F&O order, instead of the internal peer-to-peer matching engine.

Only reached via POST /createLiveOrder (api/orders.py) when
SHOONYA_LIVE_ORDERS_ENABLED=true (see live_orders_enabled() below) - the
existing POST /orders (simulated, peer-matched via ExecutionEngine) is
completely untouched, and equity orders never reach this service at all.

F&O orders routed through here stop participating in internal peer-matching
entirely: the broker is the sole source of truth for the fill. This was a
deliberate architecture decision (not both mechanisms running in parallel),
since a real broker fill and an internal peer-match both touching the same
order/position independently would corrupt positions - confirmed with the
user before building this.
"""

import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from typing import Any, Dict, Optional

from database.orderPersistence import OrderPersistence
from service.shoonyaOrderService import ShoonyaOrderService

logger = logging.getLogger(__name__)

# The broker HTTP round trip dominates this call's latency (network + the
# exchange's own processing) - 8s is generous enough to absorb a slow
# response without hanging the request indefinitely, while short enough that
# a genuinely stuck call fails fast into the "status uncertain" path below
# rather than leaving the HTTP client hanging.
ORDER_PLACEMENT_TIMEOUT_SECS = 8.0

# One pool shared by every LiveOrderRoutingService instance, not created
# fresh per instance/request - api/orders.py builds a new
# LiveOrderRoutingService on every single /createLiveOrder call, and a
# per-instance ThreadPoolExecutor that's never shut down would leak 4
# threads per request under real order volume.
_LIVE_ORDER_PLACEMENT_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="live-order-placement")

# A transient DB hiccup right after the broker has already accepted a real
# order must not immediately orphan that order (see set_broker_order_id
# retry loop below) - a few fast retries absorb a blip without meaningfully
# adding to placement latency.
SET_BROKER_ORDER_ID_MAX_ATTEMPTS = 3
SET_BROKER_ORDER_ID_RETRY_DELAY_SECS = 0.3


class LiveOrderRoutingError(Exception):
    """Base class for every error this service raises."""


class LotSizeMismatchError(LiveOrderRoutingError):
    """Quantity isn't a whole multiple of the contract's lot size - rejected
    before ever calling the broker (today nothing else in the codebase
    enforces this, so a bad quantity would otherwise reach Shoonya and come
    back as an opaque, harder-to-explain reject)."""

    def __init__(self, quantity: int, lot_size: int):
        self.quantity = quantity
        self.lot_size = lot_size
        super().__init__(f"Quantity {quantity} is not a multiple of lot size {lot_size}")


class LiveOrderRejectedError(LiveOrderRoutingError):
    """The broker explicitly rejected the order (stat: Not_Ok, with a
    reason). Safe to cancel the internal order and refund/release margin -
    the broker has confirmed nothing happened on its side, so there is
    nothing real to reconcile against."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(f"Broker rejected order: {reason}")


class LiveOrderStatusUncertainError(LiveOrderRoutingError):
    """The broker call timed out, raised an unexpected error, or returned a
    malformed success response (stat=Ok but no order number). Status is
    UNCERTAIN - the order may genuinely have gone through even though we
    didn't get clean confirmation. Callers MUST NOT treat this the same as
    a reject (must not auto-cancel/refund): doing so on an order that
    actually reached the exchange would create a real position we no longer
    have any internal record of. The internal order is left exactly as-is
    for manual reconciliation via the broker's own order book."""


def live_orders_enabled() -> bool:
    """Single source of truth for the live/simulated switch (a dedicated
    env var, independent of IS_PROD_ENVIRONMENT - simulated F&O trading must
    stay possible even in a prod deployment until this is deliberately
    turned on). Never `os.getenv(...) is True` - that comparison is always
    False regardless of the env var's value (os.getenv never returns the
    Python bool True), the exact bug already found and fixed once this
    session in service/orderService.py for IS_PROD_ENVIRONMENT."""
    return os.getenv("SHOONYA_LIVE_ORDERS_ENABLED", "false").strip().lower() == "true"


class LiveOrderRoutingService:

    def __init__(self, shoonya_api):
        """shoonya_api: the authenticated app.state.shoonya._api client.
        Caller (api/orders.py) is responsible for confirming a live session
        exists before constructing this - this class doesn't manage
        connection/auth lifecycle at all."""
        self.shoonya_order_service = ShoonyaOrderService(shoonya_api)
        self.order_persistence = OrderPersistence()
        # Shared module-level pool (not shared with position-tick/stop-trigger
        # executors elsewhere, but reused across every LiveOrderRoutingService
        # instance) - order placement is on the critical request path and
        # must never queue behind unrelated background work, and must never
        # leak a fresh 4-thread pool per request either.
        self._executor = _LIVE_ORDER_PLACEMENT_EXECUTOR
        self.logger = logger

    @staticmethod
    def validate_lot_size(quantity: int, lot_size: Optional[int]) -> None:
        if lot_size and quantity % lot_size != 0:
            raise LotSizeMismatchError(quantity, lot_size)

    def place_live_order(self, order, order_id: int, instrument: Dict[str, Any]) -> Dict[str, Any]:
        """
        Places a real order on the Shoonya master account for an F&O order
        whose internal `orders`/`order_book` rows (and any wallet debit /
        margin block) the caller has ALREADY created - this method only
        handles the broker call and persisting the resulting broker_order_id;
        it never creates or mutates the order row's core fields itself.

        Args:
            order: OrderCreate - the validated order the caller already
                persisted internally
            order_id: the internal order's id (used for logging/remarks and
                to persist broker_order_id onto)
            instrument: dict from MarginEngine.resolve_contract_type() -
                must contain at least `lot_size`

        Returns:
            {"broker_order_id": str, "status": "PENDING", "raw_response": dict}

        Raises:
            LotSizeMismatchError: quantity isn't a multiple of the lot size
            LiveOrderRejectedError: broker explicitly rejected (stat=Not_Ok)
            LiveOrderStatusUncertainError: no clean confirmation - status
                UNKNOWN, caller must NOT auto-cancel/refund
        """
        tradingsymbol = order.symbol
        lot_size = instrument.get("lot_size")
        self.validate_lot_size(order.quantity, lot_size)

        side_value = order.side.value if hasattr(order.side, "value") else str(order.side)
        product_type_value = order.product_type.value if hasattr(order.product_type, "value") else str(order.product_type)
        order_type_value = order.order_type.value if hasattr(order.order_type, "value") else str(order.order_type)
        exchange_value = order.exchange.value if hasattr(order.exchange, "value") else str(order.exchange)

        future = self._executor.submit(
            self.shoonya_order_service.place_order,
            side=side_value,
            product_type=product_type_value,
            exchange=exchange_value,
            tradingsymbol=tradingsymbol,
            quantity=order.quantity,
            order_type=order_type_value,
            price=order.price,
            trigger_price=order.trigger_price,
            remarks=f"primepip_{order_id}",
        )

        try:
            response = future.result(timeout=ORDER_PLACEMENT_TIMEOUT_SECS)
        except FutureTimeoutError as timeout_ex:
            self.logger.error(
                f"Live order placement TIMED OUT after {ORDER_PLACEMENT_TIMEOUT_SECS}s for "
                f"order_id={order_id}, symbol={tradingsymbol} - status UNCERTAIN, NOT "
                f"auto-cancelling/refunding. Manual reconciliation needed via the broker's "
                f"own order book.",
                exc_info=True,
            )
            raise LiveOrderStatusUncertainError(
                f"No broker confirmation within {ORDER_PLACEMENT_TIMEOUT_SECS}s for order {order_id}"
            ) from timeout_ex
        except Exception as ex:
            # Any other failure (network error, auth expired, mapping bug,
            # etc.) is ALSO status-uncertain, not a clean reject - never
            # auto-cancel on an ambiguous failure, same reasoning as timeout.
            self.logger.error(
                f"Live order placement raised an unexpected error for order_id={order_id}: {str(ex)}",
                exc_info=True,
            )
            raise LiveOrderStatusUncertainError(
                f"Broker call failed unexpectedly for order {order_id}: {str(ex)}"
            ) from ex

        stat = response.get("stat") if isinstance(response, dict) else None

        if stat == "Not_Ok":
            reason = response.get("emsg", "unknown reason") if isinstance(response, dict) else "no response"
            self.logger.warning(f"Broker rejected order_id={order_id}, symbol={tradingsymbol}: {reason}")
            raise LiveOrderRejectedError(reason)

        if stat != "Ok":
            # Neither Ok nor Not_Ok - a malformed/unexpected response shape.
            # Treat as uncertain rather than guessing either way.
            self.logger.error(f"Unexpected broker response for order_id={order_id}: {response}")
            raise LiveOrderStatusUncertainError(f"Unexpected broker response for order {order_id}: {response}")

        broker_order_id = response.get("norenordno")
        if not broker_order_id:
            self.logger.error(
                f"Broker returned stat=Ok but no norenordno for order_id={order_id}: {response}"
            )
            raise LiveOrderStatusUncertainError(
                f"Broker accepted order {order_id} but returned no order number"
            )

        # Persisted immediately so the order-update feed (service/
        # orderUpdateService.py) can look this order up by broker_order_id
        # the moment a fill/reject/cancel comes back - a missed write here
        # would leave a real broker order with no way to route its fill back
        # into positions/wallet, which is exactly the "db entries should not
        # be missed" requirement for this feature. Retried a few times first:
        # the broker has ALREADY accepted this order at this point, so a
        # transient DB blip here must not be treated the same as any other
        # first-attempt failure - it's the single highest-value place to
        # retry, since giving up immediately orphans a real, live order.
        persist_error: Optional[Exception] = None
        for attempt in range(1, SET_BROKER_ORDER_ID_MAX_ATTEMPTS + 1):
            try:
                self.order_persistence.set_broker_order_id(order_id, broker_order_id)
                persist_error = None
                break
            except Exception as ex:
                persist_error = ex
                self.logger.error(
                    f"Attempt {attempt}/{SET_BROKER_ORDER_ID_MAX_ATTEMPTS} to persist "
                    f"broker_order_id={broker_order_id} for order_id={order_id} failed: {ex}"
                )
                if attempt < SET_BROKER_ORDER_ID_MAX_ATTEMPTS:
                    time.sleep(SET_BROKER_ORDER_ID_RETRY_DELAY_SECS)

        if persist_error is not None:
            self.logger.critical(
                f"UNRECOVERABLE: broker ACCEPTED order_id={order_id} as "
                f"broker_order_id={broker_order_id} (symbol={tradingsymbol}, qty={order.quantity}) "
                f"but persisting broker_order_id failed after {SET_BROKER_ORDER_ID_MAX_ATTEMPTS} "
                f"attempts - THIS IS A REAL, LIVE BROKER ORDER WITH NO INTERNAL LINKAGE. "
                f"Reconcile immediately via the broker's own order book before placing anything "
                f"else for this symbol/user.",
                exc_info=True,
            )
            raise persist_error

        self.logger.info(
            f"Live order placed: order_id={order_id}, broker_order_id={broker_order_id}, "
            f"symbol={tradingsymbol}, qty={order.quantity}"
        )

        return {"broker_order_id": broker_order_id, "status": "PENDING", "raw_response": response}
