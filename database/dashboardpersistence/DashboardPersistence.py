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
        SELECT_TRADE_ORDER="""SELECT
            COUNT(*) AS total_trades,
            SUM(CASE WHEN side = 'BUY' THEN 1 ELSE 0 END) AS buy_trades,
            SUM(CASE WHEN side = 'SELL' THEN 1 ELSE 0 END) AS sell_trades
            FROM orders
            WHERE user_id = %s
        """  
        
        SELECT_USER_HOLDINGS="""
                            SELECT
                    COALESCE(SUM(p.quantity * p.avg_price), 0) AS total_invested,

                    COALESCE(SUM(p.quantity * mp.current_price), 0) AS total_holdings,

                    COALESCE(
                        SUM((mp.current_price - p.avg_price) * p.quantity),
                        0
                    ) AS unrealized_pnl,

                    COALESCE(
                        (
                            SUM(p.quantity * mp.current_price)
                            - SUM(p.quantity * p.avg_price)
                        ) * 100.0
                        / NULLIF(SUM(p.quantity * p.avg_price), 0),
                        0
                    ) AS return_percentage ,  
                    COUNT(*) AS total_positions,
                    MAX(mp.updated_at) AS last_updated

                FROM holdings p
                INNER JOIN market_prices mp
                    ON p.symbol = mp.symbol

                WHERE p.user_id = %s
                AND p.quantity > 0;
                        
        
        """
        
              
        try:
            conn=ConnectionFactory.create_connection( os.getenv("MYSQLHOST"),
                os.getenv("MYSQLUSER"),
                os.getenv("MYSQLPASSWORD"),
                os.getenv("MYSQLDATABASE"),
                os.getenv("MYSQLPORT", 3306))
            
            cursor=conn.cursor(dictionary=True)
            cursor.execute(SELECT_TOTAL_ORDERS_USERS, (userId,))
            orderCount=cursor.fetchone()
            cursor.execute(SELECT_TRADE_ORDER,(userId,))
            tradeCount=cursor.fetchone()
            cursor.execute(SELECT_USER_HOLDINGS,(userId,))
            totalHoldings=cursor.fetchone()
            dto = DashboardDTO(
                    orders=orderCount,
                    trades=tradeCount,
                    portfolio=totalHoldings,
                    last_updated=totalHoldings["last_updated"]
                )
            return dto
        except Exception as ex:
                raise  Exception(f"exception raised while fetching dashboard count: {str(ex)}")
        finally:
            if conn is not None:
                conn.close()
            if cursor is not None:
                cursor.close()
        
        
        
        
                