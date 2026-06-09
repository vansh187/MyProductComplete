from database.ConnectionFactory import ConnectionFactory
from dotenv import load_dotenv
import os

load_dotenv()
class TradeHistoryPersistence:
    def insertTradeHistoryOrders(order_id,user_id,symbol, side,quantity,execution_price,cursor):
        INSERT_TRADE_HISTORY="INSERT INTO trade_history (order_id,user_id,symbol, side,quantity,execution_price,executed_at) VALUES (%s, %s, %s, %s, %s, %s, NOW())"
        try:
            
            cursor.execute(INSERT_TRADE_HISTORY,(order_id,user_id,symbol,side,quantity,execution_price))
            transaction_id=cursor.lastrowid
            return transaction_id
        except Exception as Ex:
            raise Exception ("Error in Inserting data for trade History") from Ex
        finally:{
            print("finally block insert trade history")
        }
    
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

