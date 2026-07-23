"""
Order Service - Business logic for order operations
Production-grade code with comprehensive error handling
"""

import logging
from decimal import Decimal
from typing import Optional, List, Dict, Any

from appconfig import OptionMaster
from api.models import ExchangeType
from database.orderPersistence import OrderPersistence
from database.portfolioPersistence import portfolioPersistence
from service.tradeHistoryService import TradeHistoryService
from service.walletbalance.WalletBalanceService import WalletBalanceService
from service.marginengine.margin_engine import MarginEngine
from service.marginengine.exceptions import MarginEngineError
from dotenv import load_dotenv
import os

logger = logging.getLogger(__name__)
load_dotenv()

class OrderService:
    """Service class for order operations."""

    def __init__(self):
        """Initialize OrderService."""
        self.order_persistence = OrderPersistence()
        self.margin_engine = MarginEngine()
        self.wallet_service = WalletBalanceService()
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
            if user_id is not None:
                order.broker='Shoonya'
                order.lot_size=order.quantity

                token_match = OptionMaster.find_by_tsym(order.symbol)
                if token_match:
                    order.token = token_match["token"]
                    # Exchange must be server-resolved, not client-trusted: F&O
                    # settlement routing (is_fo_exchange in tradeSettlementService)
                    # depends on this being correct, and a client-supplied default
                    # of NSE on a derivative symbol would wrongly route a sell-to-open
                    # through the equity holdings check instead of position building.
                    # Never let an unrecognized exchange string from OptionMaster
                    # (today it only ever returns NFO/BFO, but this must not be
                    # able to break order creation if that ever changes) turn into
                    # an unhandled ValueError - fall back to the client-supplied
                    # exchange and log it instead.
                    resolved_exchange = token_match.get("exchange")
                    try:
                        order.exchange = ExchangeType(resolved_exchange)
                    except ValueError:
                        self.logger.error(
                            f"OptionMaster returned unrecognized exchange "
                            f"'{resolved_exchange}' for symbol {order.symbol}; "
                            f"keeping client-supplied exchange {order.exchange}"
                        )

                # os.getenv() always returns a str or None, never the
                # Python bool True - `os.getenv(...) is True` was always
                # False regardless of the env var's actual value, so every
                # order was silently persisted with source='SIMULATED'
                # even in a real "prod" deployment.
                if os.getenv("IS_PROD_ENVIRONMENT", "false").strip().lower() == "true":
                    order.source = 'LIVE'
                else:
                    order.source = 'SIMULATED'
                        
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

    def get_order_snapshot(self, order_id: int, cursor) -> Optional[Dict[str, Any]]:
        """
        Fetch an order's reference fields for position building, using the
        caller's own cursor/transaction.

        Args:
            order_id: Order ID
            cursor: Database cursor (shared with the calling transaction)

        Returns:
            Dict of order reference fields, or None if the order doesn't exist

        Raises:
            ValueError: If parameters are invalid
        """
        if order_id is None or order_id <= 0:
            raise ValueError("Order ID must be a positive integer")

        if cursor is None:
            raise ValueError("Database cursor cannot be None")

        try:
            return self.order_persistence.get_order_snapshot(order_id, cursor)
        except Exception as ex:
            self.logger.error(f"Error fetching order snapshot for order {order_id}: {str(ex)}")
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

            # order_details now comes straight from the cancel UPDATE's own
            # RETURNING clause (queries/orders.yaml cancel_order), not a
            # separate pre-read - code review caught that a pre-read here
            # could observe a stale price/quantity if a concurrent
            # modify_order_by_id changed them between that read and this
            # cancel actually taking effect, causing the refund below to use
            # the wrong (pre-modify) amount. Reading the cancelled values
            # from this exact statement's result closes that race - there is
            # no longer a window between "read the order" and "cancel it"
            # for another request to change price/quantity in between.
            order_details = self.order_persistence.cancel_order_by_id(user_id, order_id)
            was_cancelled = order_details is not None

            if was_cancelled:
                self.logger.info(f"Order {order_id} cancelled successfully")
                try:
                    # Idempotent no-op for non-F&O orders or orders that never
                    # had a margin block - must never block a successful
                    # cancel from being reported back to the caller.
                    self.margin_engine.release_on_cancel(order_id)
                except MarginEngineError as margin_ex:
                    self.logger.error(
                        f"Margin release failed for cancelled order {order_id} "
                        f"(cancel already succeeded): {str(margin_ex)}"
                    )

                self._refund_wallet_for_cancelled_buy_order(user_id, order_id, order_details)
            else:
                self.logger.warning(f"No pending order found to cancel: {order_id}")

            return was_cancelled

        except ValueError as val_error:
            self.logger.error(f"Validation error: {str(val_error)}")
            raise

        except Exception as ex:
            self.logger.error(f"Error cancelling order {order_id}: {str(ex)}")
            raise

    def _refund_wallet_for_cancelled_buy_order(self, user_id: int, order_id: int,
                                                order_details: Optional[Dict[str, Any]]) -> None:
        """
        Refunds the cash debited at order-creation time (api/orders.py) for
        a BUY order (equity or OPTION BUY - never FUTURES, which was never
        cash-debited in the first place) that just cancelled successfully.

        Previously nothing refunded this at all: a user who placed a BUY
        LIMIT order (debited immediately) and cancelled it before it filled
        permanently lost that cash from their wallet with no way to get it
        back - margin_engine.release_on_cancel() above only releases F&O
        margin blocks, a separate subsystem from the cash wallet debit.

        Safe to always refund the FULL quantity*price returned by the cancel
        itself: this only runs after order_persistence.cancel_order_by_id()
        actually matched a row, and that query matches status IN ('PENDING',
        'PENDING_TRIGGER') (queries/orders.yaml) - a partial fill flips
        status to PARTIALLY_EXECUTED first (service/executionEngine.py), so
        a successful cancel here is always for an order with zero fills.
        order_details comes straight from that UPDATE's own RETURNING clause
        (not a separate read), so quantity/price here are guaranteed to be
        exactly what was cancelled, even if the order had been amended by
        ticket 15's modify endpoint beforehand.

        Best-effort like the margin release above and
        _refund_wallet_after_order_creation_failure in api/orders.py: the
        cancellation itself has already committed by the time this runs,
        so a refund failure must be logged loudly (not silently swallowed)
        rather than raised - there is nothing left to roll back.
        """
        if not order_details:
            self.logger.error(
                f"Cannot refund wallet for cancelled order {order_id}: order details "
                f"were not found despite the cancel succeeding - manual reconciliation needed"
            )
            return

        side = order_details.get("side")
        side_value = side.value if hasattr(side, "value") else str(side)
        if side_value != "BUY":
            return

        price = order_details.get("price")
        quantity = order_details.get("quantity")
        if price is None or price <= 0 or quantity is None or quantity <= 0:
            return

        symbol = order_details.get("symbol")
        exchange = order_details.get("exchange")
        exchange_value = exchange.value if hasattr(exchange, "value") else str(exchange)

        try:
            instrument = self.margin_engine.resolve_contract_type(
                symbol, exchange_value, fallback_lot_size=quantity
            )
            if instrument["contract_type"] == "FUTURES":
                # Futures BUY was never cash-debited at creation - margin
                # release above already covers it.
                return

            refund_amount = Decimal(str(quantity)) * Decimal(str(price))
            self.wallet_service.creditWalletStandalone(user_id, refund_amount)
            self.logger.info(
                f"Wallet refunded for cancelled order {order_id}: user={user_id}, amount={refund_amount}"
            )
        except Exception as ex:
            self.logger.error(
                f"Failed to refund wallet of {quantity}*{price} for user {user_id} "
                f"after cancelling order {order_id} (cancel already succeeded): {str(ex)}",
                exc_info=True,
            )

    def modify_order_by_id(self, user_id: int, order_id: int, price: Optional[float] = None,
                            quantity: Optional[int] = None, trigger_price: Optional[float] = None,
                            expected_price: Optional[float] = None, expected_quantity: Optional[int] = None,
                            expected_trigger_price: Optional[float] = None) -> Dict[str, Any]:
        """
        Amend a resting order's price/quantity/trigger_price in place.

        Scope (ticket 15): only orders with status PENDING or PENDING_TRIGGER
        (zero fills so far) can be modified - a partially-filled order's
        remaining quantity/price semantics would need separate handling not
        built here. Margin-required orders (OPTION SELL, FUTURES any side)
        are rejected outright rather than half-supported: safely adjusting an
        existing margin block for a changed price/quantity needs the same
        release-then-reblock atomicity margin_engine doesn't yet expose
        (release_on_cancel and check_and_block each own their own
        transaction) - building that without a real risk of leaving a
        resting F&O order under-margined is future work, not squeezed in
        here. Cash-debited orders (BUY, non-FUTURES - same condition
        api/orders.py uses at creation) instead have their wallet debit
        adjusted by the exact price*qty delta, atomically, by the caller
        (api/orders.py) before this runs - see that call site's comments.

        expected_price/expected_quantity/expected_trigger_price: the values
        the caller read just before computing that wallet delta - passed
        straight through to the persistence layer's optimistic-concurrency
        guard (see OrderPersistence.modify_order_by_id's docstring) so two
        concurrent modify requests for the same order can't both succeed
        against the same stale base - including a trigger_price-only change,
        which previously wasn't covered by this guard at all.

        Returns:
            Dict describing what changed, for the API layer to report back.

        Raises:
            ValueError: If parameters are invalid, or no order is found.
        """
        if user_id is None or user_id <= 0:
            raise ValueError("User ID must be a positive integer")
        if order_id is None or order_id <= 0:
            raise ValueError("Order ID must be a positive integer")
        if price is None and quantity is None and trigger_price is None:
            raise ValueError("At least one of price, quantity, trigger_price must be provided")

        try:
            was_modified = self.order_persistence.modify_order_by_id(
                user_id, order_id, price, quantity, trigger_price,
                expected_price, expected_quantity, expected_trigger_price
            )
            return {"modified": was_modified}
        except Exception as ex:
            self.logger.error(f"Error modifying order {order_id}: {str(ex)}")
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

    def update_order_status_single(self, status: str, order_id: int, cursor,
                                    trigger_price: float = None, client_order_id: str = None) -> None:
        """
        Update status for a single order, along with avg_fill_price/filled_qty
        (recomputed as the weighted average across ALL fills recorded for this
        order in trade_history - not just the latest match) and, when provided,
        trigger_price/client_order_id.

        Args:
            status: New status
            order_id: Order ID
            cursor: Database cursor
            trigger_price: Optional trigger price to persist (left unchanged if None)
            client_order_id: Optional client order ID to persist (left unchanged if None)

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
            avg_fill_price, filled_qty = TradeHistoryService().getFillStats(order_id, cursor)
            portfolioPersistence.updateOrderStatusSingle(
                status, order_id, cursor, avg_fill_price, filled_qty, trigger_price, client_order_id
            )
            self.logger.info(
                f"Updated single order status: order_id={order_id}, status={status}, "
                f"avg_fill_price={avg_fill_price}, filled_qty={filled_qty}"
            )

        except Exception as ex:
            self.logger.error(f"Error updating order status: {str(ex)}")
            raise

