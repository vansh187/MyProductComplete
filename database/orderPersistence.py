from database.ConnectionFactory import ConnectionFactory
from dotenv import load_dotenv
import os

load_dotenv()

def create_order(order, user_id):
    # Here you would implement the logic to save the order to the database
    # For demonstration, we will just print the order and user_id
    INSERT_ORDER_QUERY = "INSERT INTO orders (user_id, symbol, side, quantity, price, status, created_at) VALUES (%s, %s, %s, %s, %s, %s, NOW())"
    conn = ConnectionFactory.create_connection( os.getenv("MYSQLHOST"),
            os.getenv("MYSQLUSER"),
            os.getenv("MYSQLPASSWORD"),
            os.getenv("MYSQLDATABASE"),
            os.getenv("MYSQLPORT", 3306)

    )
    cursor = conn.cursor()
    cursor.execute(INSERT_ORDER_QUERY, (user_id, order.symbol, order.side, order.quantity, order.price, order.status))
    conn.commit()
    cursor.close()
    conn.close()
    print(f"Creating order for user_id: {user_id}")
    print(f"Order details: {order}")

def get_orders(user_id):
    # Here you would implement the logic to retrieve orders from the database
    # For demonstration, we will just return a dummy list of orders
    SELECT_ORDERS_QUERY = "SELECT id, symbol, side, quantity, price, status FROM orders WHERE user_id = %s"
    conn = ConnectionFactory.create_connection( os.getenv("MYSQLHOST"),
            os.getenv("MYSQLUSER"),
            os.getenv("MYSQLPASSWORD"),
            os.getenv("MYSQLDATABASE"),
            os.getenv("MYSQLPORT", 3306)

    )
    cursor = conn.cursor()
    cursor.execute(SELECT_ORDERS_QUERY, (user_id,))
    orders = cursor.fetchall()
    cursor.close()
    conn.close()
    
    print(f"Retrieving orders for user_id: {user_id}")
    return orders


def getOrderById(userId,orderId):
     ORDER_BY_ID="SELECT id, symbol, side, quantity, price, status FROM orders WHERE user_id = %s and id=%s"
     conn = ConnectionFactory.create_connection( os.getenv("MYSQLHOST"),
            os.getenv("MYSQLUSER"),
            os.getenv("MYSQLPASSWORD"),
            os.getenv("MYSQLDATABASE"),
            os.getenv("MYSQLPORT", 3306)

        )
     cursor = conn.cursor()
     cursor.execute(ORDER_BY_ID,(userId,orderId))
     order=cursor.fetchone()
     cursor.close()
     conn.close()
     return order

def cancelOrderById(userId,orderId):
    CANCELLED="CANCELLED"
    CANCEL_ORDER_ID="UPDATE ORDERS SET STATUS= %s WHERE USER_ID=%s AND ID=%s"
    conn = ConnectionFactory.create_connection( os.getenv("MYSQLHOST"),
            os.getenv("MYSQLUSER"),
            os.getenv("MYSQLPASSWORD"),
            os.getenv("MYSQLDATABASE"),
            os.getenv("MYSQLPORT", 3306)

        )
    cursor = conn.cursor()
    cursor.execute(CANCEL_ORDER_ID,(CANCELLED,userId,orderId))
    conn.commit()
    cursor.close()
    conn.close()
    print(f"order cancelled with order id: {userId}")

