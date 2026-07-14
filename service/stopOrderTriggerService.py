"""
StopOrderTriggerService - Ticket 14: STOP/STOP-LIMIT orders don't actually
trigger.

STOP/STOPLIMIT orders are created dormant (order_book.status =
'PENDING_TRIGGER', see database/portfolioPersistence.py.createTradeinOrderBook)
so matching_engine.yaml's select_buy_orders/select_sell_orders - which filter
on status IN ('PENDING', 'PARTIALLY_EXECUTED') - never see them, and they can
never be matched before their trigger_price is actually crossed. Previously
every order type was inserted as an immediately-matchable 'LIMIT' order
regardless of order_type, so a STOP order could execute the instant it was
placed - defeating the entire purpose of a stop order and potentially filling
at a price far from what the user intended.

This service is the only place that flips a dormant STOP/STOPLIMIT order back
to PENDING (matchable) - triggered by a fresh trade price for that exact
symbol becoming available. Two call sites feed it:
  - ExecutionEngine._process_trades: every real internal trade is itself a
    trustworthy fresh price for that symbol - this works with zero external
    broker-feed dependency, which matters today since there is no reliable
    always-on live tick source in this codebase yet (see
    service/marginengine/tiered_price_resolver.py's Tier 1 gap).
  - PositionTickService.handle_tick: a bonus real-time source once a live
    broker feed is connected for a symbol with an open position.

A STOP/STOPLIMIT order resting on a symbol that never trades again (and never
gets a live tick) simply stays dormant - the same honest limitation the
margin engine's price resolver already documents and accepts.
"""

import logging
from decimal import Decimal, InvalidOperation
from typing import Optional

import psycopg2.extras

from database.PostgresConnectionFactory import PostgresConnectionFactory
from database.portfolioPersistence import portfolioPersistence

logger = logging.getLogger(__name__)


class StopOrderTriggerService:

    def __init__(self):
        self.logger = logger

    def check_and_trigger(self, symbol: str, ltp) -> None:
        """
        Checks every dormant STOP/STOPLIMIT order resting on `symbol` against
        a fresh last-traded price, and triggers (flips PENDING_TRIGGER ->
        PENDING) any whose trigger condition is met: BUY triggers when
        ltp >= trigger_price, SELL triggers when ltp <= trigger_price - the
        standard stop-order convention. Plain STOP orders (no limit price of
        their own) adopt the triggering ltp as their execution-attempt price;
        STOPLIMIT orders keep their originally-submitted limit price.

        Never raises - called from hot paths (every trade, every live tick)
        where a trigger-check failure must not block the trade/tick itself.
        A failure here is logged and the order simply stays dormant for the
        next opportunity to trigger it.
        """
        if not symbol or ltp is None:
            return

        try:
            ltp_decimal = Decimal(str(ltp))
        except (InvalidOperation, ValueError, TypeError):
            return

        triggered = []
        conn = None
        cursor = None
        try:
            conn = PostgresConnectionFactory.create_connection()
            if conn is None:
                return
            cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

            # FOR UPDATE (see select_pending_trigger_orders_by_symbol) so two
            # concurrent fresh-price signals for the same symbol can't both
            # trigger the same resting order.
            rows = portfolioPersistence.get_pending_trigger_orders_by_symbol(symbol, cursor)
            for row in rows:
                effective_price = self._trigger_condition_met(row, ltp_decimal)
                if effective_price is None:
                    continue

                portfolioPersistence.mark_order_book_triggered(row["id"], effective_price, cursor)
                portfolioPersistence.mark_orders_triggered(row["order_id"], effective_price, cursor)
                triggered.append({
                    "order_book_id": row["id"],
                    "order_id": row["order_id"],
                    "user_id": row["user_id"],
                    "symbol": row["symbol"],
                    "side": row["side"],
                    "quantity": row["remaining_quantity"],
                    "price": effective_price,
                    "trigger_price": row["trigger_price"],
                    "exchange": row["exchange"],
                    "product_type": row["product_type"],
                    "validity": row["validity"],
                    "order_type": row["order_type"],
                    "client_order_id": row["client_order_id"],
                })

            conn.commit()
        except Exception as ex:
            if conn:
                conn.rollback()
            self.logger.error(
                f"StopOrderTriggerService.check_and_trigger failed for symbol={symbol}: {str(ex)}",
                exc_info=True,
            )
            return
        finally:
            if cursor is not None:
                cursor.close()
            if conn is not None:
                conn.close()

        # Matching runs in its own transaction per triggered order, mirroring
        # ExecutionEngine.execute_order's own connection-per-order lifecycle -
        # a matching failure for one triggered order must not roll back the
        # trigger commit above, nor block any other order also triggered by
        # this same price signal.
        for trigger in triggered:
            try:
                self._run_matching_for_triggered_order(trigger)
            except Exception as ex:
                # Defense-in-depth on top of _run_matching_for_triggered_order's
                # own internal try/except: a failure here must never propagate
                # into the caller (a live trade commit or a live tick) and must
                # never block matching for any other order this same price
                # signal also triggered.
                self.logger.error(
                    f"Unhandled error running matching for triggered order "
                    f"order_id={trigger.get('order_id')}: {str(ex)}",
                    exc_info=True,
                )

    @staticmethod
    def _trigger_condition_met(row: dict, ltp: Decimal) -> Optional[Decimal]:
        trigger_price = row.get("trigger_price")
        if trigger_price is None:
            return None
        trigger_price_decimal = Decimal(str(trigger_price))
        side = (row.get("side") or "").upper()

        if side == "BUY":
            if ltp < trigger_price_decimal:
                return None
        elif side == "SELL":
            if ltp > trigger_price_decimal:
                return None
        else:
            return None

        order_type = (row.get("order_type") or "").upper()
        if order_type == "STOP":
            return ltp

        existing_price = row.get("price")
        return Decimal(str(existing_price)) if existing_price is not None else ltp

    def _run_matching_for_triggered_order(self, trigger: dict) -> None:
        from api.models import OrderCreate, OrderSide, OrderType, ExchangeType, ProductType, ValidityType
        from service.executionEngine import ExecutionEngine

        try:
            order = OrderCreate(
                symbol=trigger["symbol"],
                exchange=ExchangeType(trigger["exchange"]),
                side=OrderSide(trigger["side"]),
                quantity=trigger["quantity"],
                order_type=OrderType(trigger["order_type"]),
                price=float(trigger["price"]),
                trigger_price=float(trigger["trigger_price"]) if trigger["trigger_price"] is not None else None,
                product_type=ProductType(trigger["product_type"]),
                validity=ValidityType(trigger["validity"]),
                client_order_id=trigger["client_order_id"],
            )
            execution_engine = ExecutionEngine(order, trigger["order_id"])
            execution_engine.execute_order(
                trigger["user_id"], existing_order_book_id=trigger["order_book_id"]
            )
        except Exception as ex:
            self.logger.error(
                f"Failed to run matching for triggered order_id={trigger.get('order_id')}, "
                f"order_book_id={trigger.get('order_book_id')}: {str(ex)}",
                exc_info=True,
            )


stopOrderTriggerService = StopOrderTriggerService()
