from database.ConnectionFactory import ConnectionFactory
import os
from dotenv import load_dotenv
from productdto.DashboardDTO import DashboardDTO 


load_dotenv()
class DashBoardPersistence:
    
    
    def getDashboardDetails(self,userId):
        conn=None
        cursor =None
        
        SELECT_TOTAL_ORDERS_USERS="""SELECT
                    COUNT(*) AS total_orders,
                    SUM(CASE WHEN status = 'PENDING' THEN 1 ELSE 0 END) AS pending_orders,
                    SUM(CASE WHEN status = 'EXECUTED' THEN 1 ELSE 0 END) AS executed_orders,
                    SUM(CASE WHEN status = 'PARTIALLY_EXECUTED' THEN 1 ELSE 0 END) AS partially_executed_orders,
                    SUM(CASE WHEN status = 'CANCELLED' THEN 1 ELSE 0 END) AS cancelled_orders,
                    SUM(CASE WHEN status = 'FAILED' THEN 1 ELSE 0 END) AS failed_orders
                FROM orders
                WHERE user_id = %s"""
        try:
            conn=ConnectionFactory.create_connection( os.getenv("MYSQLHOST"),
                os.getenv("MYSQLUSER"),
                os.getenv("MYSQLPASSWORD"),
                os.getenv("MYSQLDATABASE"),
                os.getenv("MYSQLPORT", 3306))
            
            cursor=conn.cursor(dictionary=True)
            cursor.execute(SELECT_TOTAL_ORDERS_USERS, (userId,))
            orderCount=cursor.fetchone()
            dto = DashboardDTO(
                    user_id=userId,
                    orders=orderCount
                )
            return dto
        except Exception as ex:
                raise  Exception(f"exception raised while fetching dashboard count: {str(ex)}")
        finally:
            if conn is not None:
                conn.close()
            if cursor is not None:
                cursor.close()
        
        
        
        
                