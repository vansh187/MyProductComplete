from service.portfolioService import portfolioService
from service.orderService import create_order
from service.orderService import updateStatus as update_order_status
from service.tradeHistoryService import TradeHistoryService as tradeService
from dotenv import load_dotenv
from database.ConnectionFactory import ConnectionFactory
import os


load_dotenv()

class ExecutionEngine:

    @staticmethod
    def executeOrder(order, userId):
        executionPrice = order.price
        ## events to NSE gateway

        if order.status != "PENDING":
            return {
                "success": False,
                "message": f"Order already {order.status}"
            }

        try:
            conn = ConnectionFactory.create_connection(
                os.getenv("MYSQLHOST"),
                os.getenv("MYSQLUSER"),
                os.getenv("MYSQLPASSWORD"),
                os.getenv("MYSQLDATABASE"),
                os.getenv("MYSQLPORT", 3306)
            )
            cursor = conn.cursor()
            if order.side == "BUY":
                portfolioService.process_buyer(userId, order.symbol, order.quantity, executionPrice,cursor)
            elif order.side == "SELL":
                portfolioService.process_seller(userId, order.symbol, order.quantity, order.price,cursor)

            transaction_id = tradeService.insertTradeOrders(
                order.id,
                userId,
                order.symbol,
                order.side,
                order.quantity,
                order.price,
                cursor
            )

            update_order_status(userId, order.symbol, "EXECUTED",cursor)
            conn.commit()
            return {
                "success": True,
                "status": "ORDER STATUS EXECUTED",
                "tradeOrderId": transaction_id
            }

        except Exception as e:
            conn.rollback()
            update_order_status(userId, order.symbol, "FAILED",cursor)
            return {
                "success": False,
                "status": f"ORDER STATUS FAILED {e}"
            }

        finally:
            if  conn is not None:
                conn.close()
            if cursor is not None:
                cursor.close()    

