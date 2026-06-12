from service.portfolioService import portfolioService
from service.orderService import create_order
from service.orderService import updateStatus as update_order_status
from service.tradeHistoryService import TradeHistoryService as tradeService
from dotenv import load_dotenv
from database.ConnectionFactory import ConnectionFactory
import os
from service.matchingEngine.matchingEngine import MatchingEngine as MatchingEngine


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
            orderBookId = portfolioService.createTradeinOrderBook(conn,cursor,order, userId)

            # Call matching engine to find matching orders
            matchFoundList = MatchingEngine.execute(order, userId,cursor)
            matchFound=matchFoundList["tradeExcecution"]
            matchFoundObject =matchFound[0]
            if matchFound is not None:
                # Insert trade history
                transaction_id = tradeService.insertTradeOrders(
                    matchFoundObject.buy_order_id,
                    matchFoundObject.sell_order_id,
                    matchFoundObject.buy_user_id,
                    matchFoundObject.sell_user_id,
                    matchFoundObject.symbol,
                    matchFoundObject.quantity,
                    matchFoundObject.execution_price,
                    matchFoundObject.trade_value,
                    cursor
                )
                #orderbook will update the 
                #call to update remaining quantity of order book
                updatedQty=order.remainiungQty
                portfolioService.updateOrderBookQuantity(updatedQty,orderBookId,cursor)
                # Update user holdings based on order side
                 # Update order status to executed
                if updatedQty== 0:
                        status = "EXECUTED"
                else:
                    status = "PARTIALLY_EXECUTED"
                update_order_status(userId, order.symbol, status, cursor)
                if order.side == "BUY":
                    portfolioService.process_buyer(userId, order.symbol, updatedQty, executionPrice, cursor)
                elif order.side == "SELL":
                    portfolioService.process_seller(userId, order.symbol,updatedQty, order.price, cursor)

               
            if status  =='PARTIALLY_EXECUTED':
                    return{ "success": True,
                    "status": "ORDER Partially EXECUTED",
                    "tradeOrderId": transaction_id} 

            elif status  =='EXECUTED':
                     return {
                            "success": True,
                            "status": "ORDER STATUS EXECUTED",
                            "tradeOrderId": transaction_id
                }
            else:
                return {
                    "userId": userId,
                    "message": "Orders execution is in process. Please wait"
                }

        except Exception as e:
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
                conn.commit()
                conn.close()    

