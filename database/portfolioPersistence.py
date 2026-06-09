from dotenv import load_dotenv
from database.ConnectionFactory import ConnectionFactory
import os


load_dotenv()
class portfolioPersistence:
    
    def process_buyer(userId,symbol,quantity,price,cursor):
        SELECT_HOLDINGS="SELECT quantity, avg_price FROM holdings WHERE user_id = %s AND symbol = %s"
        UPDATE_HOLDINGS="UPDATE holdings SET quantity = %s, avg_price = %s WHERE user_id = %s AND symbol = %s"
        INSERT_HOLDINGS="INSERT INTO holdings (user_id, symbol, quantity, avg_price) VALUES (%s, %s, %s, %s)"
        try:
            if quantity <= 0:
                raise Exception("Quantity must be greater than zero")

            if price <= 0:
                raise Exception("Price must be greater than zero")
            
            cursor.execute(SELECT_HOLDINGS,(userId,symbol))
            holdings=cursor.fetchone()
            if  holdings:
                old_qty, old_price = holdings
                if old_qty <= 0:
                    raise Exception("Quantity must be greater than zero")
            
                new_qty = old_qty + quantity
                new_avg = ((old_qty * old_price) + (quantity * price)) / new_qty
                cursor.execute(UPDATE_HOLDINGS, (new_qty, new_avg, userId, symbol))
                
            else:
                cursor.execute(INSERT_HOLDINGS, (userId, symbol, quantity, price))    
                
        ##try:
        ##    portfolioPersistence.updateorderStatus(conn, userId, symbol)
       ## except:
           ## raise Exception("Exception in calling updateorderStatus") 
        except:
            raise Exception("Exception in inserting holdings")
        finally:
              print("Execute finally block process buy")
            


    def process_seller(userId,symbol,quantity,price,cursor):   
         SELECT_SELL_PROCESS=" SELECT quantity, avg_price FROM holdings WHERE user_id = %s AND symbol = %s"
         UPDATE_ORDER_QUANTITY="UPDATE holdings SET quantity = %s WHERE user_id = %s AND symbol = %s"
         if quantity <= 0:
            raise Exception("Quantity must be greater than zero")
         try:
           
            cursor.execute(SELECT_SELL_PROCESS,(userId,symbol))
            sellHolding=cursor.fetchone()

            if not sellHolding:
                raise Exception("No holdings found")
            old_qty, avg_price = sellHolding    
            if quantity <= 0:
                raise Exception("Quantity must be greater than zero")
            if quantity > old_qty:
                raise Exception("Not enough quantity")
         
            new_qty = old_qty - quantity

            cursor.execute(UPDATE_ORDER_QUANTITY, (new_qty, userId, symbol))
        ## try:
              ##  portfolioPersistence.updateorderStatus(conn, userId, symbol)
        ## except Exception as exc:
         ##    raise Exception("Exception in calling updateorderStatus") from exc
         except Exception as e:
             raise Exception(f"Exception in process_seller: {str(e)}")
         
         finally:
            print("Executing finally for process sell")
         
         
    
    def updateorderStatus(cursor,userId,symbol,status) :
        UPDATE_ORDER_STATUS="UPDATE orders SET status=%s WHERE user_id=%s AND symbol=%s"
        if status is None:
             cursor.execute(UPDATE_ORDER_STATUS,("EXECUTED",userId,symbol))
        else:
            cursor.execute(UPDATE_ORDER_STATUS,(status,userId,symbol))
        print("order status update success")


    def updateStatus(userId,symbol,status,cursor):
            
            portfolioPersistence.updateorderStatus(cursor, userId, symbol,status)
           

    def createUserHolding(order,userId):
         CREATE_HOLDINGS_ORDER="INSERT INTO holdings (user_id, symbol, quantity, avg_price,updated_at) VALUES (%s, %s, %s, %s,NOW())"
         try:
            conn=ConnectionFactory.create_connection(os.getenv("MYSQLHOST"),
            os.getenv("MYSQLUSER"),
            os.getenv("MYSQLPASSWORD"),
            os.getenv("MYSQLDATABASE"),
            os.getenv("MYSQLPORT", 3306))
            cursor=conn.cursor()
            cursor.execute(CREATE_HOLDINGS_ORDER,(userId,order.symbol,order.quantity,order.avg_price))
            
         except Exception as Ex:
            raise Exception("Error in inserting holdings") from Ex
         finally:
             cursor.close()
             conn.commit()
         
    def updateUserHoldings(order,userId,new_qty,round,new_avg):
         UPDATE_USER_HOLDINGS="UPDATE holdings set quantity=%s,avg_price=%s,updated_at=NOW() where user_id=%s and symbol=%s"
         try:
            conn=ConnectionFactory.create_connection(os.getenv("MYSQLHOST"),
             os.getenv("MYSQLUSER"),
             os.getenv("MYSQLPASSWORD"),
             os.getenv("MYSQLDATABASE"),
             os.getenv("MYSQLPORT", 3306))
            cursor=conn.cursor()
            cursor.execute(UPDATE_USER_HOLDINGS,(new_qty,new_avg,userId,order.symbol))
            
         except Exception as ex:
              raise Exception("Error in holdings update") from ex
         finally:
             cursor.close()
             conn.commit()