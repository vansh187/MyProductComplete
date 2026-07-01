import logging
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from utils.auth_dependency import get_current_user
from service.orderService import OrderService
from service.executionEngine import ExecutionEngine
from service.walletbalance.WalletBalanceService import WalletBalanceService

from api.models import OrderCreate, OrderSide, OrderType

logger = logging.getLogger(__name__)
router = APIRouter()

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

        # BUY order: Check wallet balance
        if order.side == OrderSide.BUY:
            if order.price is None or order.price <= 0:
                raise HTTPException(
                    status_code=400,
                    detail="BUY orders require a valid price"
                )

            required_balance = Decimal(str(order.quantity)) * order.price

            wallet_service = WalletBalanceService()
            wallet = wallet_service.getWalletBalance(user_id)

            if wallet is None:
                logger.warning(f"No wallet found for user {user_id}")
                raise HTTPException(status_code=400, detail="User wallet not initialized")

            available_balance = Decimal(str(wallet.get("balance", 0)))

            if available_balance < required_balance:
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
            raise HTTPException(status_code=500, detail="Failed to create order")

        logger.info(f"Order created: ID={order_id}, User={user_id}, Symbol={order.symbol}")

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
        raise HTTPException(status_code=500, detail="Failed to create order")


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
