from database.PostgresConnectionFactory import PostgresConnectionFactory
from utils.query_loader import QueryLoader
import psycopg2.extras
from dotenv import load_dotenv
from productdto.DashboardDTO import DashboardDTO

load_dotenv()


class DashBoardPersistence:

    def getDashboardDetails(self, userId):
        conn = None
        cursor = None
        try:
            conn = PostgresConnectionFactory.create_connection()
            cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

            cursor.execute(QueryLoader.get('dashboard.yaml', 'select_order_counts'), (userId,))
            orderCount = cursor.fetchone()

            cursor.execute(QueryLoader.get('dashboard.yaml', 'select_trade_counts'), (userId,))
            tradeCount = cursor.fetchone()

            cursor.execute(QueryLoader.get('dashboard.yaml', 'select_holdings_summary'), (userId,))
            totalHoldings = cursor.fetchone()

            dto = DashboardDTO(
                orders=orderCount,
                trades=tradeCount,
                portfolio=totalHoldings,
                last_updated=totalHoldings["last_updated"] if totalHoldings else None
            )
            return dto
        except Exception as ex:
            raise Exception(f"Exception raised while fetching dashboard count: {str(ex)}")
        finally:
            if conn is not None:
                conn.close()
            if cursor is not None:
                cursor.close()

    def getHoldingsSummaryByBucket(self, userId, asset_types):
        conn = None
        cursor = None
        try:
            conn = PostgresConnectionFactory.create_connection()
            cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cursor.execute(
                QueryLoader.get('dashboard.yaml', 'select_holdings_summary_by_bucket'),
                (userId, asset_types)
            )
            return cursor.fetchone()
        except Exception as ex:
            raise Exception(f"Exception raised while fetching bucketed holdings summary: {str(ex)}")
        finally:
            if conn is not None:
                conn.close()
            if cursor is not None:
                cursor.close()
