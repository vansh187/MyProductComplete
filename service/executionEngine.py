from service.portfolioService import portfolioService
from service.orderService import create_order
from service.orderService import updateStatus as update_order_status
from service.tradeHistoryService import TradeHistoryService as tradeService
from dotenv import load_dotenv
from database.ConnectionFactory import ConnectionFactory
import os
import traceback
from service.matchingEngine.matchingEngine import MatchingEngine 
load_dotenv()

class ExecutionEngine:

    @staticmethod
    def executeOrder( order, userId):
        """
        Execute an order for a user by:
        1. Creating an order book entry
        2. Matching the order in the matching engine
        3. Processing holdings and trade history if matched
        4. Updating order status
        """
        executionPrice = order.price

        if order.status != "PENDING":
            return {
                "success": False,
                "message": f"Order already {order.status}"
            }

        conn = None
        cursor = None
        try:
            conn = ConnectionFactory.create_connection(
                os.getenv("MYSQLHOST"),
                os.getenv("MYSQLUSER"),
                os.getenv("MYSQLPASSWORD"),
                os.getenv("MYSQLDATABASE"),
                os.getenv("MYSQLPORT", 3306)
            )
            cursor = conn.cursor(dictionary=True)

            # Create order in order book
            orderBookId = portfolioService.createTradeinOrderBook(conn,cursor,order, userId,order.id)

            # Call matching engine to find matching orders
            matchFoundList = MatchingEngine.execute(order, userId,cursor)
            tradeExecutions = matchFoundList.get("tradeExcecution", [])

            for matchFound in tradeExecutions:
                if matchFound is None:
                    continue

                # Insert trade history
                transaction_id = tradeService.insertTradeOrders(
                    matchFound.buy_order_id,
                    matchFound.sell_order_id,
                    matchFound.buy_user_id,
                    matchFound.sell_user_id,
                    matchFound.symbol,
                    matchFound.quantity,
                    matchFound.execution_price,
                    matchFound.trade_value,
                    cursor
                )
                # orderbook will update the remaining quantity of order book
                updatedQty = matchFound.remaining_qty
                

                # Update user holdings based on order side
                # Update order status to executed
                if updatedQty == 0:
                    status = "EXECUTED"
                else:
                    status = "PARTIALLY_EXECUTED"
                portfolioService.updateOrderBookQuantity(updatedQty, orderBookId, status,cursor)
                update_order_status(userId, order.symbol, status,matchFound.buy_order_id, matchFound.sell_order_id,cursor)
                message=None
                if order.side == "BUY":
                   message= portfolioService.process_buyer(userId, order.symbol, order.quantity, executionPrice, cursor)
                elif order.side == "SELL":
                    message=portfolioService.process_seller(userId, order.symbol, order.quantity, order.price, cursor)
                    if message is not None:
                        return message                       
                            
                if status == 'PARTIALLY_EXECUTED':
                    if conn is not None:
                        conn.commit()
                    return {
                        "success": True,
                        "status": "ORDER Partially EXECUTED",
                        "tradeOrderId": transaction_id
                    }
                elif status == 'EXECUTED':
                    if conn is not None:
                        conn.commit()
                    return {
                        "success": True,
                        "status": "ORDER STATUS EXECUTED",
                        "tradeOrderId": transaction_id
                    }
            conn.commit()
            return {
                "userId": userId,
                "message": "Orders execution is in process. Please wait"
            }

        except Exception as e:
            print(traceback.format_exc())
            if conn is not None:
                conn.rollback()
            if cursor is not None:
                try:
                    update_order_status(userId, order.symbol, "FAILED", cursor)
                except:
                    pass
            return {
                "success": False,
                "status": f"ORDER STATUS FAILED: {str(e)}"
            }

        finally:
            if cursor is not None:
                cursor.close()
            if conn is not None:
                conn.close()    

