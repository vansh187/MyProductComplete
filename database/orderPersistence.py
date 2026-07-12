"""
Order Persistence Layer - Database operations for orders
Production-grade code with comprehensive error handling and None checks
"""

import logging
from typing import Optional, Dict, Any, List
import psycopg2
import psycopg2.extras

from database.PostgresConnectionFactory import PostgresConnectionFactory
from database.portfolioPersistence import portfolioPersistence
from utils.query_loader import QueryLoader
from api.models import is_dormant_order_type, order_type_str

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

            # STOP/STOPLIMIT orders must not be immediately matchable - they
            # start dormant (PENDING_TRIGGER) until their trigger_price is
            # crossed by a real trade price (see StopOrderTriggerService).
            # MARKET/LIMIT orders are unaffected and keep the existing PENDING
            # behavior.
            order_type_value = order_type_str(order.order_type)
            initial_status = "PENDING_TRIGGER" if is_dormant_order_type(order.order_type) else "PENDING"

            cursor.execute(
                query,
                (
                    user_id,
                    order.symbol,
                    order.side.value,
                    order.quantity,
                    float(order.price) if order.price else None,
                    initial_status,
                    order.exchange.value,
                    order_type_value,
                    order.product_type.value,
                    order.validity.value,
                    float(order.trigger_price) if order.trigger_price else None,
                    order.client_order_id,
                    order.broker,
                    order.source,
                    order.token,
                    order.lot_size
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

    def get_order_snapshot(self, order_id: int, cursor) -> Optional[Dict[str, Any]]:
        """
        Fetch the reference fields (symbol/broker/source/token/lot_size/product_type/
        exchange) an in-flight trade needs to build a position row, using the caller's
        own cursor so it reads inside the same transaction as the trade being processed.

        Args:
            order_id: Order ID
            cursor: Database cursor (shared with the calling transaction)

        Returns:
            Dict of order reference fields, or None if the order doesn't exist

        Raises:
            ValueError: If parameters are invalid
        """
        if order_id is None or order_id <= 0:
            logger.error(f"get_order_snapshot() received invalid order_id: {order_id}")
            raise ValueError("Order ID must be a positive integer")

        if cursor is None:
            logger.error("get_order_snapshot() received None cursor")
            raise ValueError("Database cursor cannot be None")

        query = QueryLoader.get('orders.yaml', 'get_order_snapshot_by_id')
        if query is None:
            raise Exception("Query 'get_order_snapshot_by_id' not found in orders.yaml")

        cursor.execute(query, (order_id,))
        return cursor.fetchone()

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
    def cancel_order_by_id(user_id: int, order_id: int) -> Optional[Dict[str, Any]]:
        """
        Cancel a pending order.

        Args:
            user_id: User ID
            order_id: Order ID

        Returns:
            The cancelled order's fields (side, quantity, price, symbol,
            exchange) via the UPDATE's own RETURNING clause if a pending
            order was actually cancelled, None if no pending order was found.

            Returning the row straight from the atomic cancel UPDATE (rather
            than making the caller do a separate get_order_by_id read first)
            closes a race code review caught: a separate pre-read could
            observe a stale price/quantity if a concurrent modify_order_by_id
            changed them between that read and this cancel taking effect,
            causing OrderService's refund to use the wrong amount. Reading
            the values from this same statement's result guarantees they are
            exactly what was cancelled, not a stale snapshot.

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

            cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

            query = QueryLoader.get('orders.yaml', 'cancel_order')
            if query is None:
                raise Exception("Query 'cancel_order' not found in orders.yaml")

            # cancel_order now matches status IN ('PENDING', 'PENDING_TRIGGER')
            # so a not-yet-triggered STOP/STOPLIMIT order can be cancelled too,
            # and RETURNING gives back the exact row that was cancelled.
            cursor.execute(query, ("CANCELLED", user_id, order_id))

            cancelled_row = cursor.fetchone()
            cancelled = cancelled_row is not None

            if cancelled:
                # Same transaction as the orders-table cancel above: the
                # order_book row (what matching_engine.yaml's
                # select_buy_orders/select_sell_orders actually query) must be
                # cancelled too, or a "cancelled" order stays silently
                # matchable/triggerable. See cancel_order_book_by_order_id's
                # docstring for the full history of this gap.
                portfolioPersistence.cancel_order_book_by_order_id(order_id, cursor)

            conn.commit()

            if cancelled:
                logger.info(f"Order cancelled successfully: order_id={order_id}, user_id={user_id}")
            else:
                logger.warning(f"No pending order found to cancel: order_id={order_id}")

            return cancelled_row

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

    @staticmethod
    def modify_order_by_id(user_id: int, order_id: int, price: Optional[float],
                            quantity: Optional[int], trigger_price: Optional[float],
                            expected_price: Optional[float], expected_quantity: Optional[int],
                            expected_trigger_price: Optional[float] = None) -> bool:
        """
        Modify a resting (PENDING/PENDING_TRIGGER) order's price/quantity/
        trigger_price in place, keeping the same order_id (so order history
        and client_order_id stay intact - this is an amend, not a
        cancel-and-replace). Only fields that are not None are changed
        (COALESCE in both queries) - the caller is responsible for validating
        and pre-computing any wallet/margin delta BEFORE calling this, since
        by the time this runs the modification itself must be a pure data
        update with no room left to fail on funds.

        expected_price/expected_quantity/expected_trigger_price are the
        values the caller read just before computing that wallet delta - the
        UPDATE's WHERE clause (queries/orders.yaml modify_order) uses
        Postgres's IS NOT DISTINCT FROM (NULL-safe equality) to only apply if
        the row still has those exact values, i.e. optimistic concurrency
        control covering all three amendable fields. Without this, two
        concurrent modify requests for the same order would each compute
        their wallet delta (or, for a trigger_price-only change, no delta at
        all) against the same stale base, and both writes would succeed
        (only status was checked) even though only the second one's values
        actually end up persisted - double-charging/refunding the wallet, or
        silently losing one caller's trigger_price change with no error.

        Both the orders row and its order_book row are updated in the SAME
        transaction, exactly mirroring cancel_order_by_id - a modify that
        only touched `orders` would leave the matching engine (which reads
        order_book, not orders) matching against stale price/quantity.

        Returns:
            True if the order was still modifiable and was updated, False if
            it had already been matched/cancelled/failed - OR concurrently
            modified by another request - between the caller's read and this
            write (caller must then reverse any wallet/margin delta it
            already applied).
        """
        if user_id is None or user_id <= 0:
            raise ValueError("User ID must be a positive integer")
        if order_id is None or order_id <= 0:
            raise ValueError("Order ID must be a positive integer")

        conn = None
        cursor = None

        try:
            conn = PostgresConnectionFactory.create_connection()
            if conn is None:
                raise Exception("Failed to establish database connection")

            cursor = conn.cursor()

            query = QueryLoader.get('orders.yaml', 'modify_order')
            if query is None:
                raise Exception("Query 'modify_order' not found in orders.yaml")

            cursor.execute(query, (
                price, quantity, trigger_price, user_id, order_id,
                expected_price, expected_quantity, expected_trigger_price
            ))
            modified = cursor.rowcount > 0

            if modified:
                portfolioPersistence.modify_order_book_by_order_id(
                    order_id, price, quantity, quantity, trigger_price, cursor
                )

            conn.commit()

            if modified:
                logger.info(f"Order modified successfully: order_id={order_id}, user_id={user_id}")
            else:
                logger.warning(f"No modifiable order found: order_id={order_id}, user_id={user_id}")

            return modified

        except psycopg2.Error as db_error:
            if conn:
                conn.rollback()
            logger.error(f"Database error modifying order: {str(db_error)}")
            raise Exception(f"Database error: {str(db_error)}") from db_error

        except Exception as ex:
            if conn:
                conn.rollback()
            logger.error(f"Error modifying order {order_id}: {str(ex)}")
            raise Exception(f"Error modifying order: {str(ex)}") from ex

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
