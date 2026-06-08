from database.ConnectionFactory import ConnectionFactory
from dotenv import load_dotenv
import os

load_dotenv()
class TradeHistoryPersistence:
    def insertTradeHistoryOrders(order_id,user_id,symbol, side,quantity,execution_price):
        INSERT_TRADE_HISTORY="INSERT INTO trade_history (order_id,user_id,symbol, side,quantity,execution_price,executed_at) VALUES (%s, %s, %s, %s, %s, %s, NOW())"
        try:
            conn = ConnectionFactory.create_connection(
            os.getenv("MYSQLHOST"),
            os.getenv("MYSQLUSER"),
            os.getenv("MYSQLPASSWORD"),
            os.getenv("MYSQLDATABASE"),
            os.getenv("MYSQLPORT", 3306)
             )
            cursor= conn.cursor()
            cursor.execute(INSERT_TRADE_HISTORY,(order_id,user_id,symbol,side,quantity,execution_price))
            
            conn.commit()
            transaction_id=cursor.lastrowid
            cursor.close()
            conn.close()
            return transaction_id
        except Exception as Ex:
            raise Exception ("Error in Inserting data for trade History") from Ex