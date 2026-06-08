from database import orderPersistence
from database.portfolioPersistence import portfolioPersistence


def create_order(order, user_id):
    # Here you would implement the logic to save the order to the database
    # For demonstration, we will just print the order and user_id
    orderPersistence.create_order(order, user_id)    
    print(f"Creating order for user_id: {user_id}")
    print(f"Order details: {order}")

def getorders(user_id):
    # Here you would implement the logic to retrieve orders from the database
    # For demonstration, we will just return a dummy list of orders
    orders =orderPersistence.get_orders(user_id)
    
    print(f"Retrieving orders for user_id: {user_id}")
    return orders

def getOrderById(userId,orderId):
    order=orderPersistence.getOrderById(userId,orderId)
    print(f"Retrieving orders for user_id: {userId}")
    return order  

def cancelOrderById(userId,orderId):
    order=orderPersistence.cancelOrderById(userId,orderId)
    if order is None:
       print(f"only pending orders are allowed: {userId}") 
    else:
        print(f"order cancelled for user_id: {userId}")
    return order 

def updateStatus(user_id, symbol, status):
    portfolioPersistence.updateStatus(user_id, symbol, status)

