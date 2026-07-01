"""
Order Persistence Layer - Database operations for orders
Production-grade code with comprehensive error handling and None checks
"""

import logging
from typing import Optional, Dict, Any, List
import psycopg2
import psycopg2.extras

from database.PostgresConnectionFactory import PostgresConnectionFactory
from utils.query_loader import QueryLoader

logger = logging.getLogger(__name__)


class OrderPersistence:
    """Order persistence layer for database operations."""

    @staticmethod
    def create_order(order: Any, user_id: int) -> int:
        """
        Create a new order in the database.

        Args:
            order: Order object with symbol, side, quantity, price, exchange, etc.
            user_id: User ID who created the order

        Returns:
            Order ID from database

        Raises:
            ValueError: If parameters are None or invalid
            Exception: If database operation fails
        """
        if order is None:
            logger.error("create_order() received None order")
            raise ValueError("Order cannot be None")

        if user_id is None or user_id <= 0:
            logger.error(f"create_order() received invalid user_id: {user_id}")
            raise ValueError("User ID must be a positive integer")

        conn = None
        cursor = None

        try:
            conn = PostgresConnectionFactory.create_connection()
            if conn is None:
                raise Exception("Failed to establish database connection")

            cursor = conn.cursor()

            # Execute insert query with all new fields
            query = QueryLoader.get('orders.yaml', 'create_order')
            if query is None:
                raise Exception("Query 'create_order' not found in orders.yaml")

            cursor.execute(
                query,
                (
                    user_id,
                    order.symbol,
                    order.side.value,
                    order.quantity,
                    float(order.price) if order.price else None,
                    "PENDING",
                    order.exchange.value,
                    order.order_type.value,
                    order.product_type.value,
                    order.validity.value,
                    float(order.trigger_price) if order.trigger_price else None,
                    order.client_order_id
                )
            )

            row = cursor.fetchone()
            if row is None:
                raise Exception("Failed to insert order - no ID returned")

            conn.commit()
            order_id = row[0]

            logger.info(f"Order created successfully: ID={order_id}, User={user_id}, Symbol={order.symbol}")
            return order_id

        except psycopg2.Error as db_error:
            if conn:
                conn.rollback()
            logger.error(f"Database error creating order: {str(db_error)}")
            raise Exception(f"Database error: {str(db_error)}") from db_error

        except ValueError as val_error:
            if conn:
                conn.rollback()
            logger.error(f"Validation error: {str(val_error)}")
            raise

        except Exception as ex:
            if conn:
                conn.rollback()
            logger.error(f"Unexpected error creating order: {str(ex)}")
            raise Exception(f"Error creating order: {str(ex)}") from ex

        finally:
            if cursor is not None:
                cursor.close()
            if conn is not None:
                conn.close()

    @staticmethod
    def get_orders(user_id: int) -> List[Dict[str, Any]]:
        """
        Fetch all orders for a user.

        Args:
            user_id: User ID

        Returns:
            List of order dictionaries

        Raises:
            ValueError: If user_id is invalid
            Exception: If database operation fails
        """
        if user_id is None or user_id <= 0:
            logger.error(f"get_orders() received invalid user_id: {user_id}")
            raise ValueError("User ID must be a positive integer")

        conn = None
        cursor = None

        try:
            conn = PostgresConnectionFactory.create_connection()
            if conn is None:
                raise Exception("Failed to establish database connection")

            cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

            query = QueryLoader.get('orders.yaml', 'get_orders')
            if query is None:
                raise Exception("Query 'get_orders' not found in orders.yaml")

            cursor.execute(query, (user_id,))
            orders = cursor.fetchall()

            logger.info(f"Retrieved {len(orders) if orders else 0} orders for user {user_id}")
            return orders if orders else []

        except psycopg2.Error as db_error:
            logger.error(f"Database error fetching orders: {str(db_error)}")
            raise Exception(f"Database error: {str(db_error)}") from db_error

        except Exception as ex:
            logger.error(f"Error fetching orders for user {user_id}: {str(ex)}")
            raise Exception(f"Error fetching orders: {str(ex)}") from ex

        finally:
            if cursor is not None:
                cursor.close()
            if conn is not None:
                conn.close()

    @staticmethod
    def get_order_by_id(user_id: int, order_id: int) -> Optional[Dict[str, Any]]:
        """
        Fetch a single order by ID.

        Args:
            user_id: User ID
            order_id: Order ID

        Returns:
            Order dictionary or None if not found

        Raises:
            ValueError: If parameters are invalid
            Exception: If database operation fails
        """
        if user_id is None or user_id <= 0:
            logger.error(f"get_order_by_id() received invalid user_id: {user_id}")
            raise ValueError("User ID must be a positive integer")

        if order_id is None or order_id <= 0:
            logger.error(f"get_order_by_id() received invalid order_id: {order_id}")
            raise ValueError("Order ID must be a positive integer")

        conn = None
        cursor = None

        try:
            conn = PostgresConnectionFactory.create_connection()
            if conn is None:
                raise Exception("Failed to establish database connection")

            cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

            query = QueryLoader.get('orders.yaml', 'get_order_by_id')
            if query is None:
                raise Exception("Query 'get_order_by_id' not found in orders.yaml")

            cursor.execute(query, (user_id, order_id))
            order = cursor.fetchone()

            if order is None:
                logger.warning(f"Order not found: order_id={order_id}, user_id={user_id}")
            else:
                logger.info(f"Retrieved order: {order_id}")

            return order

        except psycopg2.Error as db_error:
            logger.error(f"Database error fetching order: {str(db_error)}")
            raise Exception(f"Database error: {str(db_error)}") from db_error

        except Exception as ex:
            logger.error(f"Error fetching order {order_id}: {str(ex)}")
            raise Exception(f"Error fetching order: {str(ex)}") from ex

        finally:
            if cursor is not None:
                cursor.close()
            if conn is not None:
                conn.close()

    @staticmethod
    def cancel_order_by_id(user_id: int, order_id: int) -> bool:
        """
        Cancel a pending order.

        Args:
            user_id: User ID
            order_id: Order ID

        Returns:
            True if cancelled, False if no pending order found

        Raises:
            ValueError: If parameters are invalid
            Exception: If database operation fails
        """
        if user_id is None or user_id <= 0:
            logger.error(f"cancel_order_by_id() received invalid user_id: {user_id}")
            raise ValueError("User ID must be a positive integer")

        if order_id is None or order_id <= 0:
            logger.error(f"cancel_order_by_id() received invalid order_id: {order_id}")
            raise ValueError("Order ID must be a positive integer")

        conn = None
        cursor = None

        try:
            conn = PostgresConnectionFactory.create_connection()
            if conn is None:
                raise Exception("Failed to establish database connection")

            cursor = conn.cursor()

            query = QueryLoader.get('orders.yaml', 'cancel_order')
            if query is None:
                raise Exception("Query 'cancel_order' not found in orders.yaml")

            cursor.execute(query, ("CANCELLED", user_id, order_id, "PENDING"))
            conn.commit()

            cancelled = cursor.rowcount > 0

            if cancelled:
                logger.info(f"Order cancelled successfully: order_id={order_id}, user_id={user_id}")
            else:
                logger.warning(f"No pending order found to cancel: order_id={order_id}")

            return cancelled

        except psycopg2.Error as db_error:
            if conn:
                conn.rollback()
            logger.error(f"Database error cancelling order: {str(db_error)}")
            raise Exception(f"Database error: {str(db_error)}") from db_error

        except Exception as ex:
            if conn:
                conn.rollback()
            logger.error(f"Error cancelling order {order_id}: {str(ex)}")
            raise Exception(f"Error cancelling order: {str(ex)}") from ex

        finally:
            if cursor is not None:
                cursor.close()
            if conn is not None:
                conn.close()


# Legacy function names for backward compatibility
def create_order(order, user_id):
    """Legacy wrapper for OrderPersistence.create_order()"""
    return OrderPersistence.create_order(order, user_id)


def get_orders(user_id):
    """Legacy wrapper for OrderPersistence.get_orders()"""
    return OrderPersistence.get_orders(user_id)


def getOrderById(userId, orderId):
    """Legacy wrapper for OrderPersistence.get_order_by_id()"""
    return OrderPersistence.get_order_by_id(userId, orderId)


def cancelOrderById(userId, orderId):
    """Legacy wrapper for OrderPersistence.cancel_order_by_id()"""
    was_cancelled = OrderPersistence.cancel_order_by_id(userId, orderId)
    return "Cancel Success" if was_cancelled else None
