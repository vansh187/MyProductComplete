from database.ConnectionFactory import ConnectionFactory
from dotenv import load_dotenv
import os

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
            cursor):

        INSERT_TRADE_HISTORY = """
            INSERT INTO trade_history (
                symbol,
                quantity,
                execution_price,
                executed_at,
                buy_order_id,
                sell_order_id,
                buy_user_id,
                sell_user_id,
                trade_value
            )
            VALUES (
                %s,
                %s,
                %s,
                NOW(),
                %s,
                %s,
                %s,
                %s,
                %s
            )
        """

        try:

            cursor.execute(
                INSERT_TRADE_HISTORY,
                (
                    symbol,
                    quantity,
                    execution_price,
                    buy_order_id,
                    sell_order_id,
                    buy_user_id,
                    sell_user_id,
                    trade_value
                )
            )

            transaction_id = cursor.lastrowid
            return transaction_id

        except Exception as ex:
            raise Exception(f"Error inserting trade history: {str(ex)}") from ex
        finally:
            print("finally block insert trade history")
    
    def getTradeOrdersById(userId):
        SELECT_TRADE_HISTORY="SELECT id,order_id,user_id,symbol,side,quantity,execution_price,executed_at from trade_history where user_id=%s order by executed_at DESC "
        try:
            conn=ConnectionFactory.create_connection(
            os.getenv("MYSQLHOST"),
            os.getenv("MYSQLUSER"),
            os.getenv("MYSQLPASSWORD"),
            os.getenv("MYSQLDATABASE"),
            os.getenv("MYSQLPORT", 3306)
            )

            cursor=conn.cursor()
            cursor.execute(SELECT_TRADE_HISTORY,(userId,))
            tradeOrders= cursor.fetchall()
            if tradeOrders is None:
                return None
            else:
                return tradeOrders
        except Exception as ex:
            raise Exception ("Error in Inserting data for trade History") from ex 

