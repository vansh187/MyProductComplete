from service.portfolioService import portfolioService
from service.orderService import create_order
from service.orderService import updateStatus as update_order_status

class ExecutionEngine:
    
    @staticmethod
    def executeOrder(order,userId):
        executionPrice=order.price
        ##events to NSE gateway
        try:
         if order.side == "BUY":
            portfolioService.process_buyer(userId, order.symbol, order.quantity, executionPrice)

         elif order.side == "SELL":
            portfolioService.process_seller(userId, order.symbol, order.quantity,order.price)
       
         update_order_status(
                userId,
                 order.symbol,
                "EXECUTED"
            )
         return {
                "success": True,
                "status": "ORDER STATUS EXECUTED"
            }
        except Exception as e:
             
            update_order_status(userId,order.symbol,"FAILED")
            return {
                "success": True,
                "status": "ORDER STATUS FAILED {e}"
            }


