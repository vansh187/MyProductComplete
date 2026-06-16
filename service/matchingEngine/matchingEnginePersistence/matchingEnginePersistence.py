from database.ConnectionFactory import ConnectionFactory
import os
from dotenv import load_dotenv

load_dotenv()
class matchtradeOrderforUser:
   
    def __init__(self):
         pass
   
    def matchtradingOrderforUser(self,order,status,cursor):
        
        SELECT_BUY_QUERY="SELECT *  FROM order_book WHERE symbol = %s AND side = 'SELL' AND status IN ('PENDING', 'PARTIALLY_EXECUTED') AND remaining_quantity > 0 ORDER BY price ASC, created_at DESC"
        SELECT_SELL_QURY="SELECT * FROM order_book WHERE symbol = %s AND side = 'BUY' AND status IN ('PENDING', 'PARTIALLY_EXECUTED') AND remaining_quantity > 0 ORDER BY price ASC, created_at ASC"
        try:
                  
                
                if status =='BUY':
                    cursor.execute(SELECT_SELL_QURY,(order.symbol,))
                    ordersMtached=cursor.fetchall()
                    return ordersMtached
         
                elif status =='SELL':
                    cursor.execute(SELECT_BUY_QUERY,(order.symbol,))
                    ordersMtached=cursor.fetchall()
                    return ordersMtached
                   
            
        except Exception as ex:
            raise Exception("Error in simulation of matching Order")
        
        finally:
            print("in final block of create match order for user")
          ##  if cursor is not None:
            ##    cursor.close()  
            ##if conn is not None:
              ##  conn.close()
              
                
        