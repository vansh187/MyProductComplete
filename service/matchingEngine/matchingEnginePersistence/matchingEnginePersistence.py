from database.ConnectionFactory import ConnectionFactory
import os
from dotenv import load_dotenv

load_dotenv()
class matchtradeOrderforUser:
    def matchtradeOrderforUser(order,userId,status):
        conn=None
        cursor=None
        SELECT_BUY_QUERY="""SELECT *
                            FROM order_book
                            WHERE symbol = ?
                            AND side = 'SELL'
                            AND status IN ('PENDING', 'PARTIALLY_EXECUTED')
                            AND remaining_quantity > 0
                            ORDER BY price ASC, created_at DESC"""
        SELECT_SELL_QURY="""SELECT *
                            FROM order_book
                            WHERE symbol = ?
                            AND side = 'BUY'
                            AND status IN ('PENDING', 'PARTIALLY_EXECUTED')
                            AND remaining_quantity > 0
                            ORDER BY price ASC, created_at ASC"""
        try:
                conn=   ConnectionFactory.create_connection(
                 os.getenv("MYSQLHOST"),
                 os.getenv("MYSQLUSER"),
                 os.getenv("MYSQLPASSWORD"),
                 os.getenv("MYSQLDATABASE"),
                 os.getenv("MYSQLPORT", 3306)
             )
                cursor=conn.cursor(dictionary=True)
                
                
                if status =='BUY':
                    cursor.execute(SELECT_SELL_QURY)
                    ordersMtached=cursor.fetchall()
                    return ordersMtached
         
                elif status =='SELL':
                    cursor.execute(SELECT_BUY_QUERY)
                    ordersMtached=cursor.fetchall()
                    return ordersMtached
                   
            
        except Exception as ex:
            raise Exception("Error in simulation of matching Order")
        
        finally:
            if cursor is not None:
                cursor.close()  
            if conn is not None:
                conn.close()
              
                
        