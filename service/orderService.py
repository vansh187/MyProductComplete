from database import orderPersistence
from database.portfolioPersistence import portfolioPersistence

class OrderService:
    
        def __init__(self):
             pass

        def create_order(self,order, user_id):
            # Here you would implement the logic to save the order to the database
            # For demonstration, we will just print the order and user_id
            a=orderPersistence.create_order(order, user_id)    
            print(f"Creating order for user_id: {user_id}")
            print(f"Order details: {order}")
            return a

        def getorders(self, user_id):
            orders = orderPersistence.get_orders(user_id)
            print(f"Retrieving orders for user_id: {user_id}")
            return orders

        def getOrderById(self, userId, orderId):
            order = orderPersistence.getOrderById(userId, orderId)
            print(f"Retrieving orders for user_id: {userId}")
            return order

        def cancelOrderById(self, userId, orderId):
            order = orderPersistence.cancelOrderById(userId, orderId)
            if order is None:
                print(f"only pending orders are allowed: {userId}")
            else:
                print(f"order cancelled for user_id: {userId}")
            return order

        def updateStatus(self,user_id, symbol, status,buy_order_id, sell_order_id,cursor):
            portfolioPersistence.updateStatus(user_id, symbol, status,buy_order_id, sell_order_id,cursor)

