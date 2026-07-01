"""
Execution Engine - Order execution and matching logic
Production-grade code with comprehensive error handling and no static methods
"""

import time
import logging
from typing import Dict, Any

import psycopg2
import psycopg2.extras
import psycopg2.errors

from service.portfolioService import portfolioService
from service.orderService import OrderService
from service.tradeHistoryService import TradeHistoryService as tradeService
from database.PostgresConnectionFactory import PostgresConnectionFactory
from service.matchingEngine.matchingEngine import MatchingEngine
from api.models import OrderCreate, OrderSide

MAX_MATCH_RETRIES = 3
RETRY_DELAY_SECS = 0.005

logger = logging.getLogger(__name__)


class ExecutionEngine:
    """Order execution engine with matching logic and comprehensive error handling."""

    def __init__(self, order: OrderCreate, order_id: int):
        """
        Initialize ExecutionEngine.

        Args:
            order: OrderCreate model instance
            order_id: Order ID from database

        Raises:
            ValueError: If order or order_id is invalid
        """
        if order is None:
            logger.error("ExecutionEngine.__init__() received None order")
            raise ValueError("Order cannot be None")

        if order_id is None or order_id <= 0:
            logger.error(f"ExecutionEngine.__init__() received invalid order_id: {order_id}")
            raise ValueError("Order ID must be a positive integer")

        self.order = order
        self.order_id = order_id
        self.logger = logger

    def execute_order(self, user_id: int) -> Dict[str, Any]:
        """
        Execute order with matching logic.

        Args:
            user_id: User ID who placed the order

        Returns:
            Execution result dictionary

        Raises:
            ValueError: If user_id is invalid
            Exception: If execution fails
        """
        if user_id is None or user_id <= 0:
            self.logger.error(f"execute_order() received invalid user_id: {user_id}")
            raise ValueError("User ID must be a positive integer")

        conn = None
        cursor = None

        try:
            # Phase 1: Establish database connection
            conn = PostgresConnectionFactory.create_connection()
            if conn is None:
                raise Exception("Failed to establish database connection")

            cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            if cursor is None:
                raise Exception("Failed to create database cursor")

            self.logger.info(f"Starting order execution: order_id={self.order_id}, user_id={user_id}, symbol={self.order.symbol}")

            # Phase 2: Register order in order book
            portfolio_service = portfolioService()
            if portfolio_service is None:
                raise Exception("Failed to initialize portfolio service")

            order_book_id = self._create_order_book_entry(conn, cursor, portfolio_service, user_id)

            if order_book_id is None or order_book_id <= 0:
                raise Exception("Failed to create order book entry")

            self.logger.info(f"Order book entry created: order_book_id={order_book_id}")
            conn.commit()

            # Phase 3: Execute matching with retry logic
            execution_result = self._execute_matching(conn, cursor, order_book_id, user_id)

            if execution_result is None:
                raise Exception("Matching execution returned None")

            self.logger.info(f"Order execution completed: {execution_result}")
            return execution_result

        except ValueError as val_error:
            if conn:
                conn.rollback()
            self.logger.error(f"Validation error in execute_order: {str(val_error)}")
            raise

        except psycopg2.Error as db_error:
            if conn:
                conn.rollback()
            self.logger.error(f"Database error in execute_order: {str(db_error)}")
            raise Exception(f"Database error: {str(db_error)}") from db_error

        except Exception as ex:
            if conn:
                conn.rollback()
            self.logger.error(f"Error executing order: {str(ex)}")

            # Try to mark order as FAILED
            try:
                if cursor and self.order_id:
                    order_service = OrderService()
                    order_service.update_order_status_single("FAILED", self.order_id, cursor)
                    if conn:
                        conn.commit()
            except Exception as cleanup_error:
                self.logger.error(f"Error marking order as FAILED: {str(cleanup_error)}")

            raise Exception(f"Order execution failed: {str(ex)}") from ex

        finally:
            try:
                if cursor is not None:
                    cursor.close()
            except Exception as e:
                self.logger.warning(f"Error closing cursor: {str(e)}")
            try:
                if conn is not None:
                    conn.close()
            except Exception as e:
                self.logger.warning(f"Error closing connection: {str(e)}")

    def _create_order_book_entry(self, conn, cursor, portfolio_service, user_id: int) -> int:
        """
        Create order book entry.

        Args:
            conn: Database connection
            cursor: Database cursor
            portfolio_service: Portfolio service instance
            user_id: User ID

        Returns:
            Order book ID

        Raises:
            Exception: If creation fails
        """
        if conn is None:
            raise ValueError("Connection cannot be None")

        if cursor is None:
            raise ValueError("Cursor cannot be None")

        if portfolio_service is None:
            raise ValueError("Portfolio service cannot be None")

        if user_id is None or user_id <= 0:
            raise ValueError("User ID must be a positive integer")

        try:
            order_book_id = portfolio_service.createTradeinOrderBook(
                conn, cursor, self.order, user_id, self.order_id
            )

            if order_book_id is None or order_book_id <= 0:
                raise Exception("Invalid order book ID returned")

            return order_book_id

        except Exception as ex:
            self.logger.error(f"Error creating order book entry: {str(ex)}")
            raise

    def _execute_matching(self, conn, cursor, order_book_id: int, user_id: int) -> Dict[str, Any]:
        """
        Execute matching logic with retry handling.

        Args:
            conn: Database connection
            cursor: Database cursor
            order_book_id: Order book ID
            user_id: User ID

        Returns:
            Execution result

        Raises:
            Exception: If matching fails
        """
        if conn is None:
            raise ValueError("Connection cannot be None")

        if cursor is None:
            raise ValueError("Cursor cannot be None")

        if order_book_id is None or order_book_id <= 0:
            raise ValueError("Order book ID must be a positive integer")

        if user_id is None or user_id <= 0:
            raise ValueError("User ID must be a positive integer")

        last_error = None

        for attempt in range(MAX_MATCH_RETRIES):
            try:
                self.logger.info(f"Matching attempt {attempt + 1}/{MAX_MATCH_RETRIES}")

                # Execute matching
                matching_engine = MatchingEngine(self.order, user_id, cursor)
                if matching_engine is None:
                    raise Exception("Failed to initialize matching engine")

                match_result = matching_engine.execute(self.order, user_id, cursor, self.order_id)

                if match_result is None:
                    raise Exception("Matching engine returned None")

                trade_executions = match_result.get("tradeExcecution", [])

                # No matches found
                if not trade_executions or len(trade_executions) == 0:
                    self.logger.info(f"No matches found for order {self.order_id}")
                    conn.commit()
                    return {
                        "success": True,
                        "status": "PENDING",
                        "message": "Order queued for matching",
                        "order_id": self.order_id
                    }

                # Process matched trades
                return self._process_trades(
                    conn, cursor, trade_executions, order_book_id, user_id
                )

            except psycopg2.errors.LockNotAvailable:
                self.logger.warning(f"Lock not available on attempt {attempt + 1}, retrying...")
                conn.rollback()
                last_error = "LockNotAvailable"

                if attempt < MAX_MATCH_RETRIES - 1:
                    time.sleep(RETRY_DELAY_SECS)
                continue

            except Exception as ex:
                self.logger.error(f"Error in matching attempt {attempt + 1}: {str(ex)}")
                conn.rollback()
                raise

        # All retries exhausted
        error_msg = f"Order queued after {MAX_MATCH_RETRIES} retries due to contention"
        self.logger.warning(error_msg)

        return {
            "success": True,
            "status": "QUEUED",
            "message": error_msg,
            "order_id": self.order_id
        }

    def _process_trades(self, conn, cursor, trade_executions: list,
                       order_book_id: int, user_id: int) -> Dict[str, Any]:
        """
        Process matched trades.

        Args:
            conn: Database connection
            cursor: Database cursor
            trade_executions: List of matched trades
            order_book_id: Order book ID
            user_id: User ID

        Returns:
            Trade execution result

        Raises:
            Exception: If processing fails
        """
        if conn is None:
            raise ValueError("Connection cannot be None")

        if cursor is None:
            raise ValueError("Cursor cannot be None")

        if trade_executions is None or len(trade_executions) == 0:
            raise ValueError("Trade executions cannot be empty")

        if order_book_id is None or order_book_id <= 0:
            raise ValueError("Order book ID must be a positive integer")

        if user_id is None or user_id <= 0:
            raise ValueError("User ID must be a positive integer")

        try:
            order_service = OrderService()
            portfolio_service = portfolioService()
            transaction_id = None
            total_matched_qty = 0

            self.logger.info(f"Processing {len(trade_executions)} matched trades")

            for match_found in trade_executions:
                if match_found is None:
                    self.logger.warning("Received None match")
                    continue

                # Verify user ownership of trades
                if match_found.buy_user_id == user_id or match_found.sell_user_id == user_id:
                    # Current user is one of the parties - acceptable
                    pass
                else:
                    # Security violation - user is executing trade not involving them
                    self.logger.error(f"Auth violation: user {user_id} attempted to execute trade between {match_found.buy_user_id} and {match_found.sell_user_id}")
                    raise ValueError(f"User {user_id} not authorized for this trade")

                # Track total matched quantity for incoming order
                total_matched_qty += match_found.quantity

                # Insert trade record
                transaction_id = tradeService.insertTradeOrders(
                    match_found.buy_order_id,
                    match_found.sell_order_id,
                    match_found.buy_user_id,
                    match_found.sell_user_id,
                    match_found.symbol,
                    match_found.quantity,
                    match_found.execution_price,
                    match_found.trade_value,
                    cursor,
                    user_id,
                    self.order.side.value
                )

                if transaction_id is None:
                    raise Exception("Failed to insert trade record")

                # Update counterparty order book
                counterparty_order_id = (
                    match_found.sell_order_id if self.order.side == OrderSide.BUY
                    else match_found.buy_order_id
                )
                counterparty_remaining = match_found.remaining_qty
                counterparty_status = (
                    "EXECUTED" if counterparty_remaining == 0 else "PARTIALLY_EXECUTED"
                )

                portfolio_service.updateCounterpartyOrderBook(
                    counterparty_remaining, counterparty_status, counterparty_order_id, cursor
                )

                # Update incoming order book - determine status from total matched vs order quantity
                incoming_remaining = self.order.quantity - total_matched_qty
                incoming_status = "PARTIALLY_EXECUTED" if incoming_remaining > 0 else "EXECUTED"

                portfolio_service.updateIncomingOrderBook(
                    incoming_remaining, incoming_status, order_book_id, cursor
                )

                # Update orders table
                order_service.update_order_status_single(
                    counterparty_status, counterparty_order_id, cursor
                )
                order_service.update_order_status_single(
                    incoming_status, self.order_id, cursor
                )

                # Update holdings
                if self.order.side == OrderSide.BUY:
                    portfolio_service.process_buyer(
                        user_id, self.order.symbol,
                        match_found.quantity, match_found.execution_price,
                        cursor
                    )
                elif self.order.side == OrderSide.SELL:
                    portfolio_service.process_seller(
                        user_id, self.order.symbol,
                        match_found.quantity, match_found.execution_price,
                        cursor
                    )

                self.logger.info(f"Trade processed: transaction_id={transaction_id}, qty={match_found.quantity}")

            conn.commit()
            self.logger.info(f"All trades committed successfully")

            # Determine final status
            if hasattr(self.order, 'status') and self.order.status == "PARTIALLY_EXECUTED":
                return {
                    "success": True,
                    "status": "PARTIALLY_EXECUTED",
                    "message": "Order partially executed",
                    "order_id": self.order_id,
                    "trade_id": transaction_id
                }

            return {
                "success": True,
                "status": "EXECUTED",
                "message": "Order fully executed",
                "order_id": self.order_id,
                "trade_id": transaction_id
            }

        except Exception as ex:
            self.logger.error(f"Error processing trades: {str(ex)}")
            conn.rollback()
            raise
