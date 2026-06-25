from service.portfolioService import portfolioService
from service.orderService import OrderService
from service.tradeHistoryService import TradeHistoryService as tradeService
from database.PostgresConnectionFactory import PostgresConnectionFactory
import psycopg2.extras
from dotenv import load_dotenv
import os
import traceback
from service.matchingEngine.matchingEngine import MatchingEngine

load_dotenv()


class ExecutionEngine:

    def __init__(self, order):
        self.order = order

    def executeOrder(self, order, userId):
        executionPrice = order.price
        portfolio_service = portfolioService()
        order_service = OrderService()

        if order.status != "PENDING":
            return {"success": False, "message": f"Order already {order.status}"}

        conn = None
        cursor = None
        try:
            conn = PostgresConnectionFactory.create_connection()
            cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

            orderBookId = portfolio_service.createTradeinOrderBook(conn, cursor, order, userId, order.id)

            matchingEngine = MatchingEngine(order, userId, cursor)
            matchFoundList = matchingEngine.execute(order, userId, cursor)
            tradeExecutions = matchFoundList.get("tradeExcecution", [])

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

                matchedOrderRemainingQty = matchFound.remaining_qty
                matchedOrderStatus = "EXECUTED" if matchedOrderRemainingQty == 0 else "PARTIALLY_EXECUTED"
                incomingOrderStatus = order.status  # set by matching engine: EXECUTED or PARTIALLY_EXECUTED

                portfolio_service.updateOrderBookQuantity(matchedOrderRemainingQty, orderBookId, matchedOrderStatus, matchFound.buy_order_id, matchFound.sell_order_id, cursor)
                order_service.updateStatus(userId, order.symbol, incomingOrderStatus, matchFound.buy_order_id, matchFound.sell_order_id, cursor)

                if order.side == "BUY":
                    message = portfolioService.process_buyer(userId, order.symbol, order.quantity, executionPrice, cursor)
                elif order.side == "SELL":
                    message = portfolioService.process_seller(userId, order.symbol, order.quantity, order.price, cursor)
                    if message is not None:
                        return message

                if conn is not None:
                    conn.commit()

                if incomingOrderStatus == 'PARTIALLY_EXECUTED':
                    return {"success": True, "status": "ORDER Partially EXECUTED", "tradeOrderId": transaction_id}
                elif incomingOrderStatus == 'EXECUTED':
                    return {"success": True, "status": "ORDER STATUS EXECUTED", "tradeOrderId": transaction_id}

            conn.commit()
            return {"userId": userId, "message": "Orders execution is in process. Please wait"}

        except Exception as e:
            print(traceback.format_exc())
            if conn is not None:
                conn.rollback()
            if cursor is not None:
                try:
                    order_service.updateStatus(userId, order.symbol, "FAILED", cursor)
                except:
                    pass
            return {"success": False, "status": f"ORDER STATUS FAILED: {str(e)}"}

        finally:
            if cursor is not None:
                cursor.close()
            if conn is not None:
                conn.close()
