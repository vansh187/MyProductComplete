from database.PostgresConnectionFactory import PostgresConnectionFactory
from utils.query_loader import QueryLoader
from dotenv import load_dotenv

load_dotenv()


def create_order(order, user_id):
    conn = None
    cursor = None
    try:
        conn = PostgresConnectionFactory.create_connection()
        cursor = conn.cursor()
        cursor.execute(
            QueryLoader.get('orders.yaml', 'create_order'),
            (user_id, order.symbol, order.side, order.quantity, order.price, order.status)
        )
        row = cursor.fetchone()
        conn.commit()
        print(f"Creating order for user_id: {user_id}")
        return row[0]
    except Exception as ex:
        if conn:
            conn.rollback()
        raise Exception(f"Error creating order: {str(ex)}")
    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.close()


def get_orders(user_id):
    conn = None
    cursor = None
    try:
        conn = PostgresConnectionFactory.create_connection()
        cursor = conn.cursor()
        cursor.execute(QueryLoader.get('orders.yaml', 'get_orders'), (user_id,))
        orders = cursor.fetchall()
        return orders
    except Exception as ex:
        raise Exception(f"Error fetching orders: {str(ex)}")
    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.close()


def getOrderById(userId, orderId):
    conn = None
    cursor = None
    try:
        conn = PostgresConnectionFactory.create_connection()
        cursor = conn.cursor()
        cursor.execute(QueryLoader.get('orders.yaml', 'get_order_by_id'), (userId, orderId))
        return cursor.fetchone()
    except Exception as ex:
        raise Exception(f"Error fetching order by id: {str(ex)}")
    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.close()


def cancelOrderById(userId, orderId):
    conn = None
    cursor = None
    try:
        conn = PostgresConnectionFactory.create_connection()
        cursor = conn.cursor()
        cursor.execute(
            QueryLoader.get('orders.yaml', 'cancel_order'),
            ("CANCELLED", userId, orderId, "PENDING")
        )
        conn.commit()
        if cursor.rowcount == 0:
            return
        return "Cancel Success"
    except Exception as ex:
        if conn:
            conn.rollback()
        raise Exception(f"Error cancelling order: {str(ex)}")
    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.close()
