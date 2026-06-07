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