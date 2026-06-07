from database import orderPersistence

def create_order(order, user_id):
    # Here you would implement the logic to save the order to the database
    # For demonstration, we will just print the order and user_id
    orderPersistence.create_order(order, user_id)    
    print(f"Creating order for user_id: {user_id}")
    print(f"Order details: {order}")