"""
Order Service - Business logic for order operations
Production-grade code with comprehensive error handling
"""

import logging
from typing import Optional, List, Dict, Any

from database.orderPersistence import OrderPersistence
from database.portfolioPersistence import portfolioPersistence

logger = logging.getLogger(__name__)


class OrderService:
    """Service class for order operations."""

    def __init__(self):
        """Initialize OrderService."""
        self.order_persistence = OrderPersistence()
        self.logger = logger

    def create_order(self, order: Any, user_id: int) -> int:
        """
        Create a new order.

        Args:
            order: Order object with symbol, side, quantity, price, exchange, etc.
            user_id: User ID creating the order

        Returns:
            Order ID

        Raises:
            ValueError: If parameters are invalid
            Exception: If order creation fails
        """
        if order is None:
            self.logger.error("create_order() received None order")
            raise ValueError("Order cannot be None")

        if user_id is None or user_id <= 0:
            self.logger.error(f"create_order() received invalid user_id: {user_id}")
            raise ValueError("User ID must be a positive integer")

        try:
            self.logger.info(f"Creating order for user {user_id}: {order.symbol} {order.side.value} x{order.quantity}")

            order_id = self.order_persistence.create_order(order, user_id)

            if order_id is None or order_id <= 0:
                raise Exception("Order creation returned invalid ID")

            self.logger.info(f"Order created successfully: ID={order_id}")
            return order_id

        except ValueError as val_error:
            self.logger.error(f"Validation error creating order: {str(val_error)}")
            raise

        except Exception as ex:
            self.logger.error(f"Error creating order: {str(ex)}")
            raise

    def get_orders(self, user_id: int) -> List[Dict[str, Any]]:
        """
        Retrieve all orders for a user.

        Args:
            user_id: User ID

        Returns:
            List of orders

        Raises:
            ValueError: If user_id is invalid
            Exception: If retrieval fails
        """
        if user_id is None or user_id <= 0:
            self.logger.error(f"get_orders() received invalid user_id: {user_id}")
            raise ValueError("User ID must be a positive integer")

        try:
            self.logger.info(f"Retrieving orders for user {user_id}")

            orders = self.order_persistence.get_orders(user_id)

            if orders is None:
                self.logger.warning(f"No orders found for user {user_id}")
                return []

            self.logger.info(f"Retrieved {len(orders)} orders for user {user_id}")
            return orders

        except ValueError as val_error:
            self.logger.error(f"Validation error: {str(val_error)}")
            raise

        except Exception as ex:
            self.logger.error(f"Error retrieving orders: {str(ex)}")
            raise

    def get_order_by_id(self, user_id: int, order_id: int) -> Optional[Dict[str, Any]]:
        """
        Retrieve a single order by ID.

        Args:
            user_id: User ID
            order_id: Order ID

        Returns:
            Order dictionary or None if not found

        Raises:
            ValueError: If parameters are invalid
            Exception: If retrieval fails
        """
        if user_id is None or user_id <= 0:
            self.logger.error(f"get_order_by_id() received invalid user_id: {user_id}")
            raise ValueError("User ID must be a positive integer")

        if order_id is None or order_id <= 0:
            self.logger.error(f"get_order_by_id() received invalid order_id: {order_id}")
            raise ValueError("Order ID must be a positive integer")

        try:
            self.logger.info(f"Retrieving order {order_id} for user {user_id}")

            order = self.order_persistence.get_order_by_id(user_id, order_id)

            if order is None:
                self.logger.warning(f"Order {order_id} not found for user {user_id}")

            return order

        except ValueError as val_error:
            self.logger.error(f"Validation error: {str(val_error)}")
            raise

        except Exception as ex:
            self.logger.error(f"Error retrieving order {order_id}: {str(ex)}")
            raise

    def cancel_order_by_id(self, user_id: int, order_id: int) -> bool:
        """
        Cancel a pending order.

        Args:
            user_id: User ID
            order_id: Order ID

        Returns:
            True if cancelled, False if no pending order

        Raises:
            ValueError: If parameters are invalid
            Exception: If cancellation fails
        """
        if user_id is None or user_id <= 0:
            self.logger.error(f"cancel_order_by_id() received invalid user_id: {user_id}")
            raise ValueError("User ID must be a positive integer")

        if order_id is None or order_id <= 0:
            self.logger.error(f"cancel_order_by_id() received invalid order_id: {order_id}")
            raise ValueError("Order ID must be a positive integer")

        try:
            self.logger.info(f"Cancelling order {order_id} for user {user_id}")

            was_cancelled = self.order_persistence.cancel_order_by_id(user_id, order_id)

            if was_cancelled:
                self.logger.info(f"Order {order_id} cancelled successfully")
            else:
                self.logger.warning(f"No pending order found to cancel: {order_id}")

            return was_cancelled

        except ValueError as val_error:
            self.logger.error(f"Validation error: {str(val_error)}")
            raise

        except Exception as ex:
            self.logger.error(f"Error cancelling order {order_id}: {str(ex)}")
            raise

    def update_status(self, user_id: int, symbol: str, status: str,
                     buy_order_id: int, sell_order_id: int, cursor) -> None:
        """
        Update order status for matched orders.

        Args:
            user_id: User ID
            symbol: Stock symbol
            status: New status
            buy_order_id: Buy order ID
            sell_order_id: Sell order ID
            cursor: Database cursor

        Raises:
            ValueError: If parameters are invalid
            Exception: If update fails
        """
        if user_id is None or user_id <= 0:
            raise ValueError("User ID must be a positive integer")

        if symbol is None or not symbol.strip():
            raise ValueError("Symbol cannot be empty")

        if status is None or not status.strip():
            raise ValueError("Status cannot be empty")

        if cursor is None:
            raise ValueError("Database cursor cannot be None")

        try:
            portfolioPersistence().updateStatus(user_id, symbol, status, buy_order_id, sell_order_id, cursor)
            self.logger.info(f"Updated order status: user={user_id}, symbol={symbol}, status={status}")

        except Exception as ex:
            self.logger.error(f"Error updating order status: {str(ex)}")
            raise

    def update_order_status_single(self, status: str, order_id: int, cursor) -> None:
        """
        Update status for a single order.

        Args:
            status: New status
            order_id: Order ID
            cursor: Database cursor

        Raises:
            ValueError: If parameters are invalid
            Exception: If update fails
        """
        if status is None or not status.strip():
            raise ValueError("Status cannot be empty")

        if order_id is None or order_id <= 0:
            raise ValueError("Order ID must be a positive integer")

        if cursor is None:
            raise ValueError("Database cursor cannot be None")

        try:
            portfolioPersistence().updateOrderStatusSingle(status, order_id, cursor)
            self.logger.info(f"Updated single order status: order_id={order_id}, status={status}")

        except Exception as ex:
            self.logger.error(f"Error updating order status: {str(ex)}")
            raise

