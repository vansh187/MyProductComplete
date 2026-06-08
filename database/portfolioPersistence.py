from dotenv import load_dotenv
from database.ConnectionFactory import ConnectionFactory
import os


load_dotenv()
class portfolioPersistence:
    
    def process_buyer(userId,symbol,quantity,price):
        SELECT_HOLDINGS="SELECT quantity, avg_price FROM holdings WHERE user_id = %s AND symbol = %s"
        UPDATE_HOLDINGS="UPDATE holdings SET quantity = %s, avg_price = %s WHERE user_id = %s AND symbol = %s"
        INSERT_HOLDINGS="INSERT INTO holdings (user_id, symbol, quantity, avg_price) VALUES (%s, %s, %s, %s)"
        conn=ConnectionFactory.create_connection(os.getenv("MYSQLHOST"),
            os.getenv("MYSQLUSER"),
            os.getenv("MYSQLPASSWORD"),
            os.getenv("MYSQLDATABASE"),
            os.getenv("MYSQLPORT", 3306))
        cursor=conn.cursor()
        cursor.execute(SELECT_HOLDINGS,(userId,symbol))
        holdings=cursor.fetchone()
        if  holdings:
            old_qty, old_price = holdings
            new_qty = old_qty + quantity
            new_avg = ((old_qty * old_price) + (quantity * price)) / new_qty
            cursor.execute(UPDATE_HOLDINGS, (new_qty, new_avg, userId, symbol))
        else:
            cursor.execute(INSERT_HOLDINGS, (userId, symbol, quantity, price))    
        try:
            portfolioPersistence.updateorderStatus(conn, userId, symbol)
        except:
            raise Exception("Exception in calling updateorderStatus") from exc

        conn.commit()
        cursor.close()
        conn.close()
        return holdings


    def process_seller(userId,symbol,quantity,price):   
         SELECT_SELL_PROCESS=" SELECT quantity, avg_price FROM holdings WHERE user_id = %s AND symbol = %s"
         UPDATE_ORDER_QUANTITY="UPDATE holdings SET quantity = %s WHERE user_id = %s AND symbol = %s"
         conn=ConnectionFactory.create_connection(os.getenv("MYSQLHOST"),
            os.getenv("MYSQLUSER"),
            os.getenv("MYSQLPASSWORD"),
            os.getenv("MYSQLDATABASE"),
            os.getenv("MYSQLPORT", 3306))
         cursor=conn.cursor()
         
         cursor.execute(SELECT_SELL_PROCESS,(userId,symbol))
         sellHolding=cursor.fetchone()

         if not sellHolding:
              raise Exception("No holdings found")
         old_qty, avg_price = sellHolding    
         if quantity > old_qty:
                raise Exception("Not enough quantity")
         
         new_qty = old_qty - quantity

         cursor.execute(UPDATE_ORDER_QUANTITY, (new_qty, userId, symbol))
         try:
                portfolioPersistence.updateorderStatus(conn, userId, symbol)
         except Exception as exc:
             raise Exception("Exception in calling updateorderStatus") from exc
         conn.commit()
         cursor.close()
         conn.close()
    
    def updateorderStatus(conn,userId,symbol) :
        UPDATE_ORDER_STATUS="UPDATE orders SET status=%s WHERE user_id=%s AND symbol=%s"
        cursor=conn.cursor()
        cursor.execute(UPDATE_ORDER_STATUS,("EXECUTED",userId,symbol))
        print("order status update success")