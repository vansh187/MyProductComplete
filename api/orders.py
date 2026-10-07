import asyncio
import logging
import re
import uuid
from decimal import Decimal
from typing import Any, Dict, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Request
from appconfig import OptionMaster
from appconfig.ScripMasterRefresher import upcoming_expiry_dates
from utils.auth_dependency import get_current_user
from utils.safe_numbers import safe_float, safe_int
from database.orderPersistence import OrderPersistence
from service.orderService import OrderService
from service.executionEngine import ExecutionEngine
from service.walletbalance.WalletBalanceService import WalletBalanceService
from service.marginengine.margin_engine import MarginEngine
from service.marginengine.exceptions import InsufficientMarginError, MarginEngineError, ReferencePriceUnresolvedError
from service.brokerOrderStatusService import BrokerOrderStatusService
from service.liveOrderRoutingService import (
    LiveOrderRoutingService,
    LiveOrderRejectedError,
    LiveOrderStatusUncertainError,
    LotSizeMismatchError,
    live_orders_enabled,
    sebi_algo_id,
)

from api.models import BROKER_ROUTED_SOURCE, OrderCreate, OrderModify, OrderSide, OrderType

logger = logging.getLogger(__name__)
router = APIRouter()

_broker_order_status_service = BrokerOrderStatusService()

# client_order_id travels to the broker in the order's `remarks` field, so it
# is kept to a conservative, broker-safe shape.
_CLIENT_ORDER_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
# OrderUpdateService reads "primepip_<order_id>" remarks as an internal order
# id - a client id in that shape could route another order's fill.
_RESERVED_CLIENT_ORDER_ID_PREFIX = "primepip_"


# Internal statuses of an order that is finished with zero fills - for a
# broker-routed order, nothing is (or will be) live at the exchange.
_CLOSED_UNFILLED_STATUSES = frozenset({"CANCELLED", "REJECTED", "FAILED"})


# Broker (Shoonya) order statuses after which the order can never fill.
_BROKER_CLOSED_STATUSES = frozenset({"REJECTED", "CANCELED", "CANCELLED"})


def _unseen_placement_status(internal_status: Optional[str]) -> Tuple[str, bool]:
    """(placement_status, retry_with_new_client_order_id) for a broker-routed
    order the broker does not show. Absence alone proves nothing: right
    after a timed-out placement the broker may not list it yet (and the
    order book can be up to a second old), so only our own record decides."""
    if internal_status in ("EXECUTED", "PARTIALLY_EXECUTED"):
        return "CONFIRMED", False
    if internal_status in _CLOSED_UNFILLED_STATUSES:
        # Closed by a definite broker reject (or a broker cancel/reject push)
        # with funds released - nothing can be live.
        return "CLOSED", True
    return "UNCONFIRMED", False


def _off_tick(value: Optional[float], tick_size: Decimal) -> bool:
    return value is not None and Decimal(str(value)) % tick_size != 0


def _live_order_market_rule_error(order: OrderCreate, instrument: Dict[str, Any]) -> Optional[str]:
    """Exchange rules a live F&O order is certain to be rejected for, checked
    before any DB write or wallet debit (a broker reject would otherwise cost
    a debit, a reject round trip and a refund):
      - the contract has expired (on expiry day, after the 15:30 close);
      - LIMIT/STOPLIMIT price or trigger price off the contract's tick size;
      - STOPLIMIT trigger on the wrong side of the limit price (a BUY
        stop-limit triggers at or below its limit, a SELL at or above).
    Returns the reason, or None when the order passes."""
    expiry = instrument.get("expiry")
    if expiry and not upcoming_expiry_dates([expiry]):
        return f"Contract {order.symbol} expired on {expiry} and can no longer be traded"

    order_type = _enum_value(order.order_type)
    if instrument.get("contract_type") == "OPTION":
        contract = OptionMaster.find_by_tsym(order.symbol)
        tick_size = Decimal(str(contract.get("tick_size") or OptionMaster.DEFAULT_TICK_SIZE)) if contract else None
        if tick_size:
            if order_type in ("LIMIT", "STOPLIMIT") and _off_tick(order.price, tick_size):
                return f"Price must be a multiple of the tick size ({tick_size})"
            if order_type in ("STOP", "STOPLIMIT") and _off_tick(order.trigger_price, tick_size):
                return f"Trigger price must be a multiple of the tick size ({tick_size})"

    if order_type == "STOPLIMIT" and order.price is not None and order.trigger_price is not None:
        side = _enum_value(order.side)
        if side == "BUY" and order.trigger_price > order.price:
            return "For a BUY stop-limit order the trigger price must be at or below the limit price"
        if side == "SELL" and order.trigger_price < order.price:
            return "For a SELL stop-limit order the trigger price must be at or above the limit price"
    return None


def _is_broker_routed(order_row: Dict[str, Any]) -> bool:
    """Sent to the real broker - including a timed-out placement that has no
    broker_order_id yet but may still be live at the exchange. Such orders
    must never be cancelled/amended through the internal-only endpoints."""
    return bool(order_row.get("broker_order_id")) or order_row.get("source") == BROKER_ROUTED_SOURCE


def _enum_value(value) -> Optional[str]:
    if value is None:
        return None
    return value.value if hasattr(value, "value") else str(value)


def _client_order_id_error(client_order_id: str) -> Optional[str]:
    if not _CLIENT_ORDER_ID_PATTERN.match(client_order_id):
        return "client_order_id must be 1-40 characters of letters, digits, '_' or '-'"
    if client_order_id.lower().startswith(_RESERVED_CLIENT_ORDER_ID_PREFIX):
        return f"client_order_id must not start with '{_RESERVED_CLIENT_ORDER_ID_PREFIX}'"
    return None


def _cancel_after_margin_failure(order_service: OrderService, user_id: int, order_id: int) -> None:
    """Best-effort cancel of an order that failed its post-creation margin
    check, so it isn't left resting with no margin behind it. Never raises -
    the caller is already about to raise the real HTTPException for the
    margin failure itself, and that must not be masked by a secondary
    cancellation error."""
    try:
        order_service.cancel_order_by_id(user_id, order_id)
    except Exception as ex:
        logger.error(f"Failed to auto-cancel order {order_id} after margin failure: {str(ex)}")


def _is_unique_violation(exc: BaseException) -> bool:
    """True if a Postgres unique-constraint violation (SQLSTATE 23505) is
    anywhere in the exception's cause chain - the persistence layer re-wraps
    psycopg2 errors in plain Exceptions."""
    seen = 0
    current: Optional[BaseException] = exc
    while current is not None and seen < 10:
        if getattr(current, "pgcode", None) == "23505":
            return True
        current = current.__cause__ or current.__context__
        seen += 1
    return False


def _release_after_broker_reject(order_service: OrderService, user_id: int, order_id: int,
                                 client_order_id: Optional[str]) -> None:
    """Cancels an order the broker refused (nothing exists at the exchange),
    which refunds the wallet debit and releases the margin block. Unlike the
    best-effort margin-failure cancel, nothing here is swallowed: if the
    cancel or any part of the release fails - or the order is somehow still
    open afterwards - the client gets a 500 needing manual reconciliation
    instead of being told the order was cleanly rejected."""
    try:
        if not order_service.cancel_rejected_order(user_id, order_id):
            # Not pending any more: fine only if something else (the broker's
            # own reject/cancel push - OrderUpdateService) already closed it.
            current = order_service.get_order_by_id(user_id, order_id)
            current_status = _enum_value(current.get("status")) if current else None
            if current_status not in _CLOSED_UNFILLED_STATUSES:
                raise RuntimeError(f"order is {current_status}, not closed, after the broker rejected it")
    except Exception as ex:
        logger.error(
            f"Broker rejected order {order_id} for user {user_id}, but cancelling it to release "
            f"its wallet debit/margin failed - manual reconciliation needed: {ex}",
            exc_info=True,
        )
        raise HTTPException(
            status_code=500,
            detail={
                "message": "The broker rejected the order, but releasing its blocked funds failed - "
                           "it needs manual reconciliation",
                "order_id": order_id,
                "client_order_id": client_order_id,
            },
        )


def _refund_wallet_after_order_creation_failure(wallet_service: WalletBalanceService, user_id: int, amount: Decimal) -> None:
    """Best-effort refund of a wallet debit taken atomically before order
    creation, for the rare case order creation itself then fails (a DB
    error unrelated to funds). Never raises - the caller is already about
    to raise the real 500 for the order-creation failure, and that must
    not be masked by a secondary refund error. A refund failure here is
    logged at ERROR (not swallowed silently) since it leaves a user
    debited for an order that was never created."""
    try:
        wallet_service.creditWalletStandalone(user_id, amount)
    except Exception as ex:
        logger.error(
            f"Failed to refund wallet debit of {amount} for user {user_id} after order creation failed: {str(ex)}",
            exc_info=True,
        )


# ============================================
# API ENDPOINTS
# ============================================

@router.get("/orders")
def get_orders(current_user=Depends(get_current_user)):
    """
    Retrieve all orders for the authenticated user.

    Args:
        current_user: Authenticated user from token

    Returns:
        JSON response with orders list

    Raises:
        HTTPException: If retrieval fails
    """
    try:
        if current_user is None or "user_id" not in current_user:
            logger.error("get_orders() received invalid current_user")
            raise HTTPException(status_code=401, detail="Unauthorized")

        user_id = current_user["user_id"]

        order_service = OrderService()
        orders = order_service.get_orders(user_id)

        return {
            "success": True,
            "message": "Orders retrieved successfully",
            "user_id": user_id,
            "orders": orders or []
        }

    except ValueError as val_error:
        logger.error(f"Validation error in get_orders: {str(val_error)}")
        raise HTTPException(status_code=400, detail=str(val_error))

    except Exception as ex:
        logger.error(f"Error retrieving orders: {str(ex)}")
        raise HTTPException(status_code=500, detail="Failed to retrieve orders")


def _create_order_row_with_checks(order: OrderCreate, user_id: int, broker_routed: bool = False) -> Tuple[int, Optional[str], Dict[str, Any], MarginEngine, OrderService]:
    """
    Shared by both POST /orders (simulated, peer-matched) and POST
    /createLiveOrder (real Shoonya order) - this is every DB-writing step
    that happens regardless of how the order is ultimately executed: F&O
    contract resolution, wallet debit-for-cash-BUY, the orders-table row
    itself, and F&O margin check+block. Factored out (rather than
    copy-pasted into the new live-order endpoint) so a future fix to any of
    this logic can't be applied to one endpoint and missed on the other.

    broker_routed: True only from POST /createLiveOrder (see
    OrderService.create_order) - never derived from the request body.

    Raises HTTPException directly on any failure (funds/margin/creation),
    exactly as create_order always has - callers should let it propagate
    straight to FastAPI, not catch it.

    Returns:
        (order_id, contract_type, instrument, margin_engine, order_service)
    """
    # F&O classification, resolved up front so both the BUY and SELL
    # branches below know whether this order needs the margin engine
    # (OPTION/FUTURES) instead of - or in addition to - the cash-based
    # wallet checks. contract_type is None for equity/unrecognized
    # instruments, in which case behavior is completely unchanged from
    # before the margin engine existed.
    margin_engine = MarginEngine()
    instrument = margin_engine.resolve_contract_type(
        order.symbol, order.exchange.value if hasattr(order.exchange, "value") else str(order.exchange),
        fallback_lot_size=order.quantity,
    )
    contract_type = instrument["contract_type"]
    side_value = order.side.value if hasattr(order.side, "value") else str(order.side)

    # BUY order: atomically check-and-debit the wallet in a single
    # statement, before creating the order.
    #
    # Previously this was an unlocked read-then-compare pre-check here,
    # followed by a SEPARATE unlocked read-then-compute-then-write debit
    # after order creation (below, now removed) that didn't even
    # re-verify sufficiency at write time - two concurrent BUY orders
    # could both pass the pre-check and both debit, overspending.
    # debitWalletIfSufficient() (WalletBalancePersistence) closes this
    # with a single `UPDATE ... SET balance = balance - %s WHERE
    # balance >= %s`: Postgres serializes concurrent UPDATEs to the same
    # row, so the second concurrent debit's sufficiency check is
    # evaluated against the first debit's already-committed balance,
    # never a stale read - and it costs one round trip instead of two
    # reads plus a write.
    #
    # FUTURES BUY orders are excluded from this cash-debit path entirely:
    # a future has no premium, so debiting quantity*price as if it were
    # a cash purchase would be wrong - futures margin (both BUY and
    # SELL) is handled below via MarginEngine.check_and_block() instead.
    # OPTION BUY orders (opening or closing a short) keep this path
    # unchanged - buying an option, including buying one back to cover
    # a short, always costs real premium cash.
    wallet_debited = False
    required_balance = None
    wallet_service = WalletBalanceService()
    if order.side == OrderSide.BUY and contract_type != "FUTURES":
        if order.price is None or order.price <= 0:
            raise HTTPException(
                status_code=400,
                detail="BUY orders require a valid price"
            )

        required_balance = Decimal(str(order.quantity)) * Decimal(str(order.price))

        if wallet_service.debitWalletIfSufficient(user_id, required_balance):
            wallet_debited = True
            logger.info(f"Wallet debited: user={user_id}, amount={required_balance}")
        else:
            # Debit didn't apply (insufficient funds or no wallet row) -
            # only now do a plain read, purely to build a precise error
            # message; the outcome itself was already determined
            # atomically above, so this read isn't racy with anything.
            wallet = wallet_service.getWalletBalance(user_id)
            if wallet is None:
                logger.warning(f"No wallet found for user {user_id}")
                raise HTTPException(status_code=400, detail="User wallet not initialized")

            available_balance = Decimal(str(wallet.get("balance") or 0))
            logger.warning(
                f"Insufficient balance: user={user_id}, "
                f"required={required_balance}, available={available_balance}"
            )
            raise HTTPException(
                status_code=400,
                detail=f"Insufficient balance. Required: ₹{required_balance:.2f}, Available: ₹{available_balance:.2f}"
            )

    # Create order in database
    order_service = OrderService()
    try:
        order_id = order_service.create_order(order, user_id, broker_routed=broker_routed)
    except Exception as create_ex:
        # The wallet was debited above, before the insert - a raised insert
        # must refund it just like a failed (None) one does.
        if wallet_debited:
            _refund_wallet_after_order_creation_failure(wallet_service, user_id, required_balance)
        if _is_unique_violation(create_ex):
            # orders_client_order_id_key (client_order_id is unique across
            # ALL users): either a concurrent retry won the race between our
            # per-user duplicate pre-check and this insert, or another user's
            # order already uses this id.
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "This client_order_id is already in use",
                    "client_order_id": order.client_order_id,
                },
            )
        raise

    if order_id is None or order_id <= 0:
        if wallet_debited:
            _refund_wallet_after_order_creation_failure(wallet_service, user_id, required_balance)
        raise HTTPException(status_code=500, detail="Failed to create order")

    logger.info(f"Order created: ID={order_id}, User={user_id}, Symbol={order.symbol}")

    # F&O margin check + block, for any order the margin engine actually
    # requires margin for (options SELL, futures BUY/SELL). Runs after
    # order creation because the margin block row references order_id.
    # On any margin failure the just-created order is cancelled (mirrors
    # the existing fail-fast behavior of the equity wallet check above)
    # so no order is left resting without margin behind it.
    #
    # order.exchange is authoritative here: OrderService.create_order()
    # (just called above) server-resolves it from OptionMaster for any
    # symbol it recognizes (see service/orderService.py) - the same
    # exchange TradeSettlementService later uses to route fills.
    margin_check_exchange = order.exchange.value if hasattr(order.exchange, "value") else str(order.exchange)

    if margin_engine.is_margin_required(margin_check_exchange, side_value, contract_type):
        try:
            margin_check = margin_engine.check_and_block(order, order_id, user_id)
            logger.info(
                f"Margin check passed: order={order_id}, user={user_id}, "
                f"required_margin={margin_check.required_margin}"
            )
        except InsufficientMarginError as margin_ex:
            logger.warning(f"Insufficient margin for order {order_id}, user {user_id}: {str(margin_ex)}")
            _cancel_after_margin_failure(order_service, user_id, order_id)
            raise HTTPException(
                status_code=400,
                detail=f"Insufficient margin. Required: ₹{margin_ex.required_margin:.2f}, "
                       f"Available: ₹{margin_ex.available_balance:.2f}, Shortfall: ₹{margin_ex.shortfall:.2f}"
            )
        except ReferencePriceUnresolvedError as margin_ex:
            logger.error(f"Reference price unresolved for order {order_id}, user {user_id}: {str(margin_ex)}")
            _cancel_after_margin_failure(order_service, user_id, order_id)
            raise HTTPException(
                status_code=400,
                detail=f"Could not resolve a reliable reference price for {margin_ex.tsym}. Please try again."
            )
        except MarginEngineError as margin_ex:
            logger.error(f"Margin engine error for order {order_id}, user {user_id}: {str(margin_ex)}")
            _cancel_after_margin_failure(order_service, user_id, order_id)
            raise HTTPException(status_code=500, detail="Failed to process margin for this order")

    return order_id, contract_type, instrument, margin_engine, order_service


@router.post("/orders")
def create_order(order: OrderCreate, current_user=Depends(get_current_user)):

    """
    Create a new order with wallet balance validation.

    Args:
        order: OrderCreate model instance
        current_user: Authenticated user from token

    Returns:
        Order execution result

    Raises:
        HTTPException: If validation or creation fails
    """
    try:
        # Validate current_user
        if current_user is None or "user_id" not in current_user:
            logger.error("create_order() received invalid current_user")
            raise HTTPException(status_code=401, detail="Unauthorized")

        user_id = current_user["user_id"]

        # Validate order
        if order is None:
            logger.error("create_order() received None order")
            raise HTTPException(status_code=400, detail="Order cannot be None")

        order_id, contract_type, instrument, margin_engine, order_service = _create_order_row_with_checks(order, user_id)

        # Execute order
        execution_engine = ExecutionEngine(order, order_id)
        execution_result = execution_engine.execute_order(user_id)

        if execution_result is None:
            logger.error(f"Execution engine returned None for order {order_id}")
            raise HTTPException(status_code=500, detail="Order execution failed")

        return {
            "success": True,
            "order_id": order_id,
            "execution": execution_result
        }

    except HTTPException:
        raise

    except ValueError as val_error:
        logger.error(f"Validation error: {str(val_error)}")
        raise HTTPException(status_code=400, detail=str(val_error))

    except Exception as ex:
        logger.error(f"Error creating order: {str(ex)}")
        logger.error(f"Exception type: {type(ex).__name__}")
        logger.error(f"Full traceback:", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to create order: {str(ex)}")


@router.post("/createLiveOrder")
def create_live_order(request: Request, order: OrderCreate, current_user=Depends(get_current_user)):
    """
    Places a REAL order on the Shoonya master account for an F&O
    (OPTION/FUTURES) order - separate from POST /orders (which always
    peer-matches internally) so the existing simulated path is completely
    untouched and this live path can be reasoned about/rolled back
    independently.

    Gated by SHOONYA_LIVE_ORDERS_ENABLED (see
    service/liveOrderRoutingService.live_orders_enabled) - a single .env
    switch between real and simulated. F&O orders placed through here do
    NOT participate in internal peer-matching at all: the broker is the
    sole source of truth for the fill (see service/orderUpdateService.py),
    which is why this is a dedicated endpoint rather than a mode of the
    existing one.

    Reuses _create_order_row_with_checks (same wallet/margin/order-creation
    logic as POST /orders) so both endpoints stay in sync automatically.

    Raises:
        HTTPException: If disabled, not F&O, quantity isn't a lot-size
            multiple, funds/margin fail, or the broker call itself fails
    """
    try:
        if current_user is None or "user_id" not in current_user:
            logger.error("create_live_order() received invalid current_user")
            raise HTTPException(status_code=401, detail="Unauthorized")

        user_id = current_user["user_id"]

        if order is None:
            raise HTTPException(status_code=400, detail="Order cannot be None")

        if not live_orders_enabled():
            raise HTTPException(
                status_code=400,
                detail="Live order routing is currently disabled (SHOONYA_LIVE_ORDERS_ENABLED is not set to true)"
            )

        shoonya = getattr(request.app.state, "shoonya", None)
        shoonya_api = getattr(shoonya, "_api", None) if shoonya is not None else None
        if shoonya_api is None or not getattr(shoonya, "is_connected", False):
            logger.error("create_live_order() called with no live Shoonya session")
            raise HTTPException(status_code=503, detail="Shoonya session is not connected - cannot place a live order")

        # Optional, server-side only (never client-supplied): attached when
        # SHOONYA_ALGO_ID is configured, otherwise the order is sent without
        # one, exactly as the broker library does by default.
        algo_id = sebi_algo_id()

        # Every live order gets a client_order_id (generated when the caller
        # sent none) - it is the order's tag in the broker's own order book.
        if order.client_order_id:
            order.client_order_id = order.client_order_id.strip()
            client_id_error = _client_order_id_error(order.client_order_id)
            if client_id_error:
                raise HTTPException(status_code=400, detail=client_id_error)
            existing_order = OrderPersistence().get_order_by_client_order_id(user_id, order.client_order_id)
            if existing_order is not None:
                # Idempotency: one client_order_id identifies exactly one
                # order attempt, final once used - a retry must never place a
                # second real order. A rejected attempt is closed for good
                # (the orders table also keeps the id unique), so retrying
                # after a reject needs a new id - the detail says which case
                # this is.
                existing_status = _enum_value(existing_order.get("status"))
                raise HTTPException(
                    status_code=409,
                    detail={
                        "message": "An order with this client_order_id already exists",
                        "order_id": existing_order.get("id"),
                        "client_order_id": order.client_order_id,
                        "status": existing_status,
                        "retry_with_new_client_order_id": existing_status in _CLOSED_UNFILLED_STATUSES,
                    },
                )
        else:
            order.client_order_id = f"pp{uuid.uuid4().hex[:20]}"

        # Pre-check contract type and lot size BEFORE any DB writes/wallet
        # debit, so a request for an equity symbol or a bad quantity fails
        # cleanly with zero side effects - no order row, no wallet touch,
        # nothing to unwind.
        exchange_value = order.exchange.value if hasattr(order.exchange, "value") else str(order.exchange)
        margin_engine_probe = MarginEngine()
        instrument_probe = margin_engine_probe.resolve_contract_type(
            order.symbol, exchange_value, fallback_lot_size=order.quantity
        )
        if instrument_probe["contract_type"] not in ("OPTION", "FUTURES"):
            raise HTTPException(
                status_code=400,
                detail="createLiveOrder only supports F&O (OPTION/FUTURES) symbols - use POST /orders for equity"
            )

        lot_size = instrument_probe.get("lot_size")
        if lot_size and order.quantity % lot_size != 0:
            raise HTTPException(
                status_code=400,
                detail=f"Quantity must be a multiple of the lot size ({lot_size})"
            )

        market_rule_error = _live_order_market_rule_error(order, instrument_probe)
        if market_rule_error:
            raise HTTPException(status_code=400, detail=market_rule_error)

        order_id, contract_type, instrument, margin_engine, order_service = _create_order_row_with_checks(
            order, user_id, broker_routed=True
        )

        live_order_routing_service = LiveOrderRoutingService(shoonya_api)
        try:
            live_result = live_order_routing_service.place_live_order(order, order_id, instrument, algo_id=algo_id)
        except LotSizeMismatchError as lot_ex:
            # Re-checked here since instrument (from _create_order_row_with_checks,
            # which re-resolves the contract independently of instrument_probe
            # above) is the authoritative source used for the actual placement -
            # the pre-check above is a fast-fail convenience, not the only guard.
            _release_after_broker_reject(order_service, user_id, order_id, order.client_order_id)
            raise HTTPException(
                status_code=400,
                detail={
                    "message": str(lot_ex),
                    "order_id": order_id,
                    "client_order_id": order.client_order_id,
                    "retry_with_new_client_order_id": True,
                },
            )
        except LiveOrderRejectedError as reject_ex:
            # Broker explicitly said no - safe to cancel, which reuses the
            # existing cancel path's wallet refund + margin release +
            # order_book cancellation (OrderService.cancel_order_by_id) so
            # nothing is left inconsistent.
            _release_after_broker_reject(order_service, user_id, order_id, order.client_order_id)
            raise HTTPException(
                status_code=400,
                detail={
                    "message": f"Broker rejected the order: {reject_ex.reason}",
                    "order_id": order_id,
                    "client_order_id": order.client_order_id,
                    # Nothing exists at the broker and the funds are back -
                    # a retry is safe, under a new client_order_id.
                    "retry_with_new_client_order_id": True,
                },
            )
        except LiveOrderStatusUncertainError as uncertain_ex:
            # Deliberately NOT cancelled - the order may have actually gone
            # through at the broker even though we didn't get clean
            # confirmation. Left exactly as-is for manual reconciliation.
            logger.error(
                f"Live order status uncertain for order_id={order_id}, user={user_id}: {str(uncertain_ex)}",
                exc_info=True,
            )
            raise HTTPException(
                status_code=202,
                detail={
                    "message": "Order was submitted but broker confirmation timed out - it may be live "
                               "at the exchange. Poll GET /orders/by-client-id/{client_order_id}; do NOT "
                               "retry under a new client_order_id until placement_status says it is safe.",
                    "order_id": order_id,
                    "client_order_id": order.client_order_id,
                    "retry_with_new_client_order_id": False,
                },
            )

        logger.info(
            f"Live order created: order_id={order_id}, broker_order_id={live_result['broker_order_id']}, "
            f"client_order_id={order.client_order_id}, user={user_id}, symbol={order.symbol}"
        )

        return {
            "success": True,
            "order_id": order_id,
            "client_order_id": order.client_order_id,
            "broker_order_id": live_result["broker_order_id"],
            "status": live_result["status"],
        }

    except HTTPException:
        raise

    except ValueError as val_error:
        logger.error(f"Validation error in create_live_order: {str(val_error)}")
        raise HTTPException(status_code=400, detail=str(val_error))

    except Exception as ex:
        logger.error(f"Error creating live order: {str(ex)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to create live order: {str(ex)}")


@router.get("/orders/by-client-id/{client_order_id}")
async def get_order_by_client_order_id(client_order_id: str, request: Request, current_user=Depends(get_current_user)):
    """
    The broker's own status for one of the caller's live orders, looked up by
    the client_order_id it was placed with (POST /createLiveOrder).

    Response:
    {
      "success": true, "order_id": 812, "client_order_id": "algo-42",
      "broker_order_id": "26100600012345", "internal_status": "EXECUTED",
      "placement_status": "CONFIRMED", "retry_with_new_client_order_id": false,
      "status": "COMPLETE", "filled_qty": 75, "avg_fill_price": 101.15,
      "rejreason": null, "broker_time": "10:15:30 06-10-2026"
    }
    status is the broker's (OPEN, COMPLETE, REJECTED, CANCELED,
    TRIGGER_PENDING, ...), null when the broker shows no such order.
    filled_qty/avg_fill_price come from the broker when it has the order,
    otherwise from our own order record.

    placement_status - what a caller may safely conclude:
      CONFIRMED    the order reached the exchange (broker has it, or it filled)
      CLOSED       finished with zero fills; nothing is live
      UNCONFIRMED  sent, but not visible at the broker (yet): it MAY still be
                   live - keep polling, never re-place under a new id
      NOT_ROUTED   a POST /orders order; never sent to the broker
    retry_with_new_client_order_id is true only when a new attempt cannot
    double up a live order.

    Raises:
        HTTPException: 400 bad id, 404 no such order for this user, 409 more
            than one broker order fits, 503/504 broker unreachable,
            500 unexpected failure
    """
    if current_user is None or "user_id" not in current_user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    user_id = current_user["user_id"]

    client_order_id = (client_order_id or "").strip()
    if not _CLIENT_ORDER_ID_PATTERN.match(client_order_id):
        raise HTTPException(status_code=400, detail="Invalid client_order_id")

    try:
        loop = asyncio.get_running_loop()
        order_row = await loop.run_in_executor(
            None, OrderPersistence().get_order_by_client_order_id, user_id, client_order_id
        )
    except Exception as ex:
        logger.error(f"Order lookup by client_order_id={client_order_id} failed: {ex}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to look up order")

    if order_row is None:
        raise HTTPException(status_code=404, detail="Order not found")

    internal_status = _enum_value(order_row.get("status"))
    response = {
        "success": True,
        "order_id": order_row.get("id"),
        "client_order_id": client_order_id,
        "broker_order_id": order_row.get("broker_order_id"),
        "internal_status": internal_status,
        "routed_to_broker": True,
        "broker_found": False,
        "placement_status": None,
        "retry_with_new_client_order_id": False,
        "status": None,
        "filled_qty": safe_int(order_row.get("filled_qty"), 0),
        "avg_fill_price": safe_float(order_row.get("avg_fill_price")),
        "rejreason": None,
        "broker_time": None,
    }

    shoonya = getattr(request.app.state, "shoonya", None)
    broker_status, reason = await _broker_order_status_service.get_status(shoonya, order_row)
    if broker_status is not None:
        response.update(broker_status)
        response["broker_order_id"] = broker_status.get("broker_order_id") or response["broker_order_id"]
        response["broker_found"] = True
        closed_at_broker = broker_status.get("status") in _BROKER_CLOSED_STATUSES and not broker_status.get("filled_qty")
        response["placement_status"] = "CLOSED" if closed_at_broker else "CONFIRMED"
        response["retry_with_new_client_order_id"] = closed_at_broker
        return response
    if reason == "not_routed_to_broker":
        response["routed_to_broker"] = False
        response["placement_status"] = "NOT_ROUTED"
        response["retry_with_new_client_order_id"] = internal_status in _CLOSED_UNFILLED_STATUSES
        return response
    if reason == "not_found_at_broker":
        response["placement_status"], response["retry_with_new_client_order_id"] = _unseen_placement_status(internal_status)
        return response
    if reason == "ambiguous_at_broker":
        raise HTTPException(status_code=409, detail="More than one broker order matches this order - needs manual reconciliation")
    if reason == "broker_timeout":
        raise HTTPException(status_code=504, detail="Broker did not respond in time")
    if reason in ("shoonya_disconnected", "broker_unavailable"):
        raise HTTPException(status_code=503, detail="Broker order status is unavailable right now")
    raise HTTPException(status_code=500, detail="Failed to fetch broker order status")


@router.get("/orders/{order_id}")
def get_order_by_id(order_id: int, current_user=Depends(get_current_user)):
    """
    Retrieve a specific order by ID.

    Args:
        order_id: Order ID
        current_user: Authenticated user from token

    Returns:
        Order details

    Raises:
        HTTPException: If order not found or retrieval fails
    """
    try:
        if current_user is None or "user_id" not in current_user:
            logger.error("get_order_by_id() received invalid current_user")
            raise HTTPException(status_code=401, detail="Unauthorized")

        user_id = current_user["user_id"]

        if order_id is None or order_id <= 0:
            logger.error(f"get_order_by_id() received invalid order_id: {order_id}")
            raise HTTPException(status_code=400, detail="Invalid order ID")

        order_service = OrderService()
        order = order_service.get_order_by_id(user_id, order_id)

        if order is None:
            logger.warning(f"Order not found: {order_id} for user {user_id}")
            raise HTTPException(status_code=404, detail="Order not found")

        return {
            "success": True,
            "message": "Order retrieved successfully",
            "order": order
        }

    except HTTPException:
        raise

    except ValueError as val_error:
        logger.error(f"Validation error: {str(val_error)}")
        raise HTTPException(status_code=400, detail=str(val_error))

    except Exception as ex:
        logger.error(f"Error retrieving order {order_id}: {str(ex)}")
        raise HTTPException(status_code=500, detail="Failed to retrieve order")


@router.post("/orders/{order_id}/cancel")
def cancel_order(order_id: int, current_user=Depends(get_current_user)):
    """
    Cancel a pending order.

    Args:
        order_id: Order ID to cancel
        current_user: Authenticated user from token

    Returns:
        Cancellation result

    Raises:
        HTTPException: If cancellation fails
    """
    try:
        if current_user is None or "user_id" not in current_user:
            logger.error("cancel_order() received invalid current_user")
            raise HTTPException(status_code=401, detail="Unauthorized")

        user_id = current_user["user_id"]

        if order_id is None or order_id <= 0:
            logger.error(f"cancel_order() received invalid order_id: {order_id}")
            raise HTTPException(status_code=400, detail="Invalid order ID")

        order_service = OrderService()
        existing_order = order_service.get_order_by_id(user_id, order_id)
        if existing_order is not None and _is_broker_routed(existing_order):
            # A real, live Shoonya order - this endpoint only ever touches
            # internal state (wallet refund/margin release/order_book), it
            # never calls the broker's own cancel_order. Cancelling "cleanly"
            # here while the real order stays resting at the exchange would
            # let it fill for real later with the wallet already refunded -
            # a direct path to an unfunded real position. Blocked outright
            # until live-order cancel is built, rather than half-supported.
            raise HTTPException(
                status_code=400,
                detail="This order was routed to the real broker and cannot be cancelled here yet. "
                       "Live-order cancellation isn't supported - manage it directly via the broker."
            )

        was_cancelled = order_service.cancel_order_by_id(user_id, order_id)

        if not was_cancelled:
            logger.info(f"No pending order to cancel: {order_id} for user {user_id}")
            raise HTTPException(
                status_code=400,
                detail="Only pending orders can be cancelled"
            )

        logger.info(f"Order cancelled successfully: {order_id} for user {user_id}")

        return {
            "success": True,
            "message": "Order cancelled successfully",
            "order_id": order_id
        }

    except HTTPException:
        raise

    except ValueError as val_error:
        logger.error(f"Validation error: {str(val_error)}")
        raise HTTPException(status_code=400, detail=str(val_error))

    except Exception as ex:
        logger.error(f"Error cancelling order {order_id}: {str(ex)}")
        raise HTTPException(status_code=500, detail="Failed to cancel order")


@router.put("/orders/{order_id}")
def modify_order(order_id: int, modify: OrderModify, current_user=Depends(get_current_user)):
    """
    Amend a resting (PENDING/PENDING_TRIGGER) order's price/quantity/
    trigger_price in place - ticket 15.

    Scope: only orders with zero fills so far can be amended. Margin-required
    orders (OPTION SELL, FUTURES) are rejected - see OrderService.
    modify_order_by_id's docstring for why. Cash-debited BUY orders
    (equity/OPTION BUY, non-FUTURES - the same condition create_order uses)
    have their wallet debit atomically adjusted by the exact price*quantity
    delta before the row itself is updated; if the underlying order turns
    out to have already been matched/cancelled by the time of the actual
    write (a race lost to the matching engine), that wallet delta is
    reversed and the request fails with 409 rather than silently debiting/
    crediting for a modification that never took effect.

    Raises:
        HTTPException: If validation, funds, or the modification itself fails
    """
    try:
        if current_user is None or "user_id" not in current_user:
            logger.error("modify_order() received invalid current_user")
            raise HTTPException(status_code=401, detail="Unauthorized")

        user_id = current_user["user_id"]

        if order_id is None or order_id <= 0:
            raise HTTPException(status_code=400, detail="Invalid order ID")

        if modify.price is None and modify.quantity is None and modify.trigger_price is None:
            raise HTTPException(status_code=400, detail="At least one of price, quantity, trigger_price must be provided")

        order_service = OrderService()
        existing_order = order_service.get_order_by_id(user_id, order_id)

        if existing_order is None:
            raise HTTPException(status_code=404, detail="Order not found")

        if _is_broker_routed(existing_order):
            # Same reasoning as cancel_order() above - modifying only the
            # internal row would desync from the real resting order at the
            # broker (wrong price/qty tracked internally while the real
            # order fills on its original terms). Blocked outright.
            raise HTTPException(
                status_code=400,
                detail="This order was routed to the real broker and cannot be modified here yet. "
                       "Live-order modification isn't supported - cancel/replace directly via the broker."
            )

        status_value = existing_order.get("status")
        status_value = status_value.value if hasattr(status_value, "value") else str(status_value)
        if status_value not in ("PENDING", "PENDING_TRIGGER"):
            raise HTTPException(
                status_code=400,
                detail="Only pending (unfilled) orders can be modified"
            )

        symbol = existing_order.get("symbol")
        side = existing_order.get("side")
        side_value = side.value if hasattr(side, "value") else str(side)
        exchange = existing_order.get("exchange")
        exchange_value = exchange.value if hasattr(exchange, "value") else str(exchange)
        existing_price = existing_order.get("price")
        existing_quantity = existing_order.get("quantity")
        existing_trigger_price = existing_order.get("trigger_price")

        new_quantity = modify.quantity if modify.quantity is not None else existing_quantity
        margin_engine = MarginEngine()
        instrument = margin_engine.resolve_contract_type(symbol, exchange_value, fallback_lot_size=new_quantity)
        contract_type = instrument["contract_type"]

        if margin_engine.is_margin_required(exchange_value, side_value, contract_type):
            raise HTTPException(
                status_code=400,
                detail="Modifying a margin-required (F&O) order isn't supported yet - cancel it and place a new order instead"
            )

        wallet_service = WalletBalanceService()
        wallet_delta_applied = Decimal("0")

        if side_value == "BUY" and contract_type != "FUTURES":
            new_price = modify.price if modify.price is not None else existing_price
            if new_price is None or new_price <= 0:
                raise HTTPException(status_code=400, detail="BUY orders require a valid price")

            existing_price_decimal = Decimal(str(existing_price)) if existing_price is not None else Decimal("0")
            old_required = Decimal(str(existing_quantity)) * existing_price_decimal
            new_required = Decimal(str(new_quantity)) * Decimal(str(new_price))
            delta = new_required - old_required

            if delta > 0:
                if not wallet_service.debitWalletIfSufficient(user_id, delta):
                    raise HTTPException(
                        status_code=400,
                        detail=f"Insufficient balance to increase order value by ₹{delta:.2f}"
                    )
                wallet_delta_applied = delta
            elif delta < 0:
                wallet_service.creditWalletStandalone(user_id, -delta)
                wallet_delta_applied = delta

        try:
            result = order_service.modify_order_by_id(
                user_id, order_id,
                price=modify.price, quantity=modify.quantity, trigger_price=modify.trigger_price,
                expected_price=existing_price, expected_quantity=existing_quantity,
                expected_trigger_price=existing_trigger_price
            )
        except Exception:
            _reverse_wallet_delta(wallet_service, user_id, wallet_delta_applied)
            raise

        if not result.get("modified"):
            # Lost a race against the matching engine (order got matched or
            # cancelled), OR against another concurrent modify request for
            # the same order (the optimistic-concurrency guard on
            # expected_price/expected_quantity didn't match) - either way,
            # reverse whatever wallet delta was already applied, since the
            # modification itself never took effect.
            _reverse_wallet_delta(wallet_service, user_id, wallet_delta_applied)
            raise HTTPException(
                status_code=409,
                detail="Order could not be modified - it may have already been executed or cancelled"
            )

        logger.info(f"Order modified successfully: {order_id} for user {user_id}")

        return {
            "success": True,
            "message": "Order modified successfully",
            "order_id": order_id
        }

    except HTTPException:
        raise

    except ValueError as val_error:
        logger.error(f"Validation error: {str(val_error)}")
        raise HTTPException(status_code=400, detail=str(val_error))

    except Exception as ex:
        logger.error(f"Error modifying order {order_id}: {str(ex)}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to modify order")


def _reverse_wallet_delta(wallet_service: WalletBalanceService, user_id: int, delta_applied: Decimal) -> None:
    """Undoes the wallet delta applied in modify_order() when the underlying
    order turned out not to be modifiable after all. Never raises - logged
    loudly instead, same rationale as _refund_wallet_after_order_creation_failure."""
    if delta_applied == 0:
        return
    try:
        if delta_applied > 0:
            wallet_service.creditWalletStandalone(user_id, delta_applied)
        else:
            wallet_service.debitWalletIfSufficient(user_id, -delta_applied)
    except Exception as ex:
        logger.error(
            f"Failed to reverse wallet delta of {delta_applied} for user {user_id} "
            f"after a lost modify-order race: {str(ex)}",
            exc_info=True,
        )
