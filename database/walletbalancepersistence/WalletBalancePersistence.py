from database.PostgresConnectionFactory import PostgresConnectionFactory
from utils.query_loader import QueryLoader
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()


class WalletBalancePersistence:

    def __init__(self):
        pass

    def getWalletBalance(self, userId):
        conn = None
        cursor = None
        try:
            conn = PostgresConnectionFactory.create_connection()
            if conn is None:
                raise Exception("Database connection could not be established")
            cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cursor.execute(QueryLoader.get('wallet.yaml', 'get_wallet_balance'), (userId,))
            return cursor.fetchone()
        except Exception as ex:
            raise Exception(f"Exception while fetching wallet balance: {str(ex)}")
        finally:
            if conn is not None:
                conn.close()
            if cursor is not None:
                cursor.close()
