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
            if cursor is not None:
                cursor.close()
            if conn is not None:
                conn.close()

    def getWalletBalanceWithLock(self, cursor, userId):
        """
        Get wallet balance with row-level lock for transaction safety.
        Must be called within an active transaction context.

        Args:
            cursor: Active database cursor from transaction
            userId: User ID to fetch wallet for

        Returns:
            Wallet row or None if not found

        Raises:
            Exception: If query fails
        """
        if cursor is None:
            raise ValueError("Cursor cannot be None")
        if userId is None or userId <= 0:
            raise ValueError("User ID must be a positive integer")

        try:
            cursor.execute(
                QueryLoader.get('wallet.yaml', 'get_wallet_balance_for_update'),
                (userId,)
            )
            return cursor.fetchone()
        except Exception as ex:
            raise Exception(f"Error fetching wallet with lock: {str(ex)}") from ex
