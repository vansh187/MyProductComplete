from service.portfolioService import portfolioService
from service.orderService import create_order
from service.orderService import updateStatus as update_order_status
from service.tradeHistoryService import TradeHistoryService as tradeService
class ExecutionEngine:
    
    @staticmethod
    def executeOrder(order,userId):
        executionPrice=order.price
        ##events to NSE gateway
        try:
         if order.status != "PENDING":
            return {
                "success": False,
                "message": f"Order already {order.status}"
            }   
         elif  order.side == "BUY":
            portfolioService.process_buyer(userId, order.symbol, order.quantity, executionPrice)

         elif order.side == "SELL":
            portfolioService.process_seller(userId, order.symbol, order.quantity,order.price)
         
         transaction_id=tradeService.insertTradeOrders(order.id, userId, order.symbol, order.side, order.quantity, order.price)
         
         update_order_status(
                userId,
                 order.symbol,
                "EXECUTED"
            )
         return {
                "success": True,
                "status": "ORDER STATUS EXECUTED",
                "tradeOrderId": transaction_id
            }
        except Exception as e:
             
            update_order_status(userId,order.symbol,"FAILED")
            return {
                "success": True,
                "status": "ORDER STATUS FAILED {e}"
            }


