import time
import traceback
import psycopg2
import psycopg2.extras
import psycopg2.errors
from dotenv import load_dotenv

from service.portfolioService import portfolioService
from service.orderService import OrderService
from service.tradeHistoryService import TradeHistoryService as tradeService
from database.PostgresConnectionFactory import PostgresConnectionFactory
from service.matchingEngine.matchingEngine import MatchingEngine

load_dotenv()

MAX_MATCH_RETRIES = 3
RETRY_DELAY_SECS = 0.005


class ExecutionEngine:

    def __init__(self, order):
        self.order = order

    def executeOrder(self, order, userId):
        portfolio_service = portfolioService()
        order_service = OrderService()

        if order.status != "PENDING":
            return {"success": False, "message": f"Order already {order.status}"}

        conn = None
        cursor = None
        try:
            conn = PostgresConnectionFactory.create_connection()
            cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

            # Phase 1: register the order in the order book and commit so it's visible to matching
            orderBookId = portfolio_service.createTradeinOrderBook(conn, cursor, order, userId, order.id)
            conn.commit()

            # Phase 2: matching with NOWAIT retry loop
            last_error = None
            for attempt in range(MAX_MATCH_RETRIES):
                try:
                    matchingEngine = MatchingEngine(order, userId, cursor)
                    matchFoundList = matchingEngine.execute(order, userId, cursor)
                    tradeExecutions = matchFoundList.get("tradeExcecution", [])

                    if not tradeExecutions:
                        conn.commit()
                        return {"userId": userId, "message": "Orders execution is in process. Please wait"}

                    transaction_id = None
                    for matchFound in tradeExecutions:
                        if matchFound is None:
                            continue

                        transaction_id = tradeService.insertTradeOrders(
                            matchFound.buy_order_id,
                            matchFound.sell_order_id,
                            matchFound.buy_user_id,
                            matchFound.sell_user_id,
                            matchFound.symbol,
                            matchFound.quantity,
                            matchFound.execution_price,
                            matchFound.trade_value,
                            cursor,
                            userId,
                            order.side
                        )

                        # Counterparty order_book update (by orders.id FK)
                        counterparty_order_id = (
                            matchFound.sell_order_id if order.side == "BUY"
                            else matchFound.buy_order_id
                        )
                        counterparty_remaining = matchFound.remaining_qty
                        counterparty_status = "EXECUTED" if counterparty_remaining == 0 else "PARTIALLY_EXECUTED"
                        portfolio_service.updateCounterpartyOrderBook(
                            counterparty_remaining, counterparty_status, counterparty_order_id, cursor
                        )

                        # Incoming order_book update (by order_book.id PK)
                        incoming_status = order.status  # set by matching engine on the order object
                        portfolio_service.updateIncomingOrderBook(
                            order.quantity if incoming_status == "PARTIALLY_EXECUTED" else 0,
                            incoming_status,
                            orderBookId,
                            cursor
                        )

                        # Update orders table status for both sides
                        order_service.updateOrderStatusSingle(counterparty_status, counterparty_order_id, cursor)
                        order_service.updateOrderStatusSingle(incoming_status, order.id, cursor)

                        # Update holdings portfolio using trade values from matchFound
                        if order.side == "BUY":
                            portfolioService.process_buyer(
                                userId, order.symbol,
                                matchFound.quantity, matchFound.execution_price,
                                cursor
                            )
                        elif order.side == "SELL":
                            portfolioService.process_seller(
                                userId, order.symbol,
                                matchFound.quantity, matchFound.execution_price,
                                cursor
                            )

                    conn.commit()

                    if order.status == "PARTIALLY_EXECUTED":
                        return {"success": True, "status": "ORDER Partially EXECUTED", "tradeOrderId": transaction_id}
                    return {"success": True, "status": "ORDER STATUS EXECUTED", "tradeOrderId": transaction_id}

                except psycopg2.errors.LockNotAvailable:
                    conn.rollback()
                    last_error = "LockNotAvailable"
                    if attempt < MAX_MATCH_RETRIES - 1:
                        time.sleep(RETRY_DELAY_SECS)
                    continue

            # All retries exhausted due to lock contention
            return {"userId": userId, "message": "Order queued — contention on book, will retry shortly"}

        except Exception as e:
            print(traceback.format_exc())
            if conn is not None:
                conn.rollback()
            try:
                order_service.updateOrderStatusSingle("FAILED", order.id, cursor)
                conn.commit()
            except Exception:
                pass
            return {"success": False, "status": f"ORDER STATUS FAILED: {str(e)}"}

        finally:
            if cursor is not None:
                cursor.close()
            if conn is not None:
                conn.close()
