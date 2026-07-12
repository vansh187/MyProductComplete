import logging
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from utils.auth_dependency import get_current_user
from service.orderService import OrderService
from service.executionEngine import ExecutionEngine
from service.walletbalance.WalletBalanceService import WalletBalanceService
from service.marginengine.margin_engine import MarginEngine
from service.marginengine.exceptions import InsufficientMarginError, MarginEngineError, ReferencePriceUnresolvedError

from api.models import OrderCreate, OrderSide, OrderType

logger = logging.getLogger(__name__)
router = APIRouter()


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
        order_id = order_service.create_order(order, user_id)

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
