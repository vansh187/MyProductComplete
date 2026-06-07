from database import orderPersistence

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
