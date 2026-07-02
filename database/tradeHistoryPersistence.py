from database.PostgresConnectionFactory import PostgresConnectionFactory
from utils.query_loader import QueryLoader
from dotenv import load_dotenv

load_dotenv()


class TradeHistoryPersistence:

    def insertTradeHistoryOrders(
            buy_order_id,
            sell_order_id,
            buy_user_id,
            sell_user_id,
            symbol,
            quantity,
            execution_price,
            trade_value,
            cursor,
            user_id,
            side):
        try:
            cursor.execute(
                QueryLoader.get('trade_history.yaml', 'insert_trade_history'),
                (user_id, symbol, side, quantity, execution_price,
                 buy_order_id, sell_order_id,
                 buy_user_id, sell_user_id, trade_value)
            )
            row = cursor.fetchone()
            if row is None:
                raise Exception("Failed to insert trade record - no ID returned")
            return row['id']
        except Exception as ex:
            raise Exception(f"Error inserting trade history: {str(ex)}") from ex

    def getTradeOrdersById(userId):
        conn = None
        cursor = None
        try:
            conn = PostgresConnectionFactory.create_connection()
            cursor = conn.cursor()
            cursor.execute(
                QueryLoader.get('trade_history.yaml', 'get_trade_history_by_user'),
                (userId,)
            )
            tradeOrders = cursor.fetchall()
            return tradeOrders if tradeOrders else None
        except Exception as ex:
            raise Exception(f"Error fetching trade history: {str(ex)}") from ex
        finally:
            if cursor is not None:
                cursor.close()
            if conn is not None:
                conn.close()
