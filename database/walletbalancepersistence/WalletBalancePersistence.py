from database.PostgresConnectionFactory import PostgresConnectionFactory
from utils.query_loader import QueryLoader
import psycopg2.extras
from decimal import Decimal
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

    def debitWalletIfSufficient(self, userId, amount) -> bool:
        """
        Atomically checks-and-debits a wallet in a single statement:
        `UPDATE wallets SET balance = balance - %s WHERE user_id = %s AND
        balance >= %s`. Returns True if the row matched (sufficient funds,
        debited) or False if it didn't (insufficient funds, or no wallet
        row at all) - either way, no partial/incorrect state is possible.

        This replaces the previous pattern of reading the balance
        unlocked, comparing in Python, then writing a separately-computed
        total back - two concurrent debits for the same user (two BUY
        orders placed back-to-back) could both read the same balance,
        both pass the sufficiency check, and both debit, letting a user
        spend money they don't have. A `SELECT ... FOR UPDATE` followed by
        an application-level check-then-write (the pattern
        getWalletBalanceWithLock/creditWallet use for credits, where no
        sufficiency check is needed) would also fix the race, but costs an
        extra round trip; a conditional UPDATE enforces the invariant and
        performs the debit in one round trip, and is provably race-free
        because Postgres serializes concurrent UPDATEs to the same row -
        the second concurrent debit's WHERE clause is evaluated against
        the first debit's already-committed balance, not a stale read.
        """
        if userId is None or userId <= 0:
            raise ValueError("User ID must be a positive integer")
        if amount is None or amount <= 0:
            raise ValueError("Debit amount must be positive")

        conn = None
        cursor = None
        try:
            conn = PostgresConnectionFactory.create_connection()
            cursor = conn.cursor()
            cursor.execute(
                QueryLoader.get('wallet.yaml', 'debit_wallet_balance_if_sufficient'),
                (amount, userId, amount)
            )
            debited = cursor.rowcount > 0
            conn.commit()
            return debited
        except Exception as ex:
            if conn:
                conn.rollback()
            raise Exception(f"Exception while debiting wallet balance: {str(ex)}") from ex
        finally:
            if cursor is not None:
                cursor.close()
            if conn is not None:
                conn.close()

    def creditWalletStandalone(self, userId, amount) -> None:
        """
        Atomic credit (`balance = balance + %s`) that manages its own
        connection/transaction, for callers with no existing open cursor -
        e.g. refunding a debit made by debitWalletIfSufficient() if order
        creation subsequently fails. Race-free for the same reason the
        Razorpay wallet-credit fix is: a single-statement delta update has
        no read step that can go stale.
        """
        if userId is None or userId <= 0:
            raise ValueError("User ID must be a positive integer")
        if amount is None or amount <= 0:
            raise ValueError("Credit amount must be positive")

        conn = None
        cursor = None
        try:
            conn = PostgresConnectionFactory.create_connection()
            cursor = conn.cursor()
            cursor.execute(
                QueryLoader.get('wallet.yaml', 'increment_wallet_balance'),
                (amount, userId)
            )
            conn.commit()
        except Exception as ex:
            if conn:
                conn.rollback()
            raise Exception(f"Exception while crediting wallet balance: {str(ex)}") from ex
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

    def creditWallet(self, cursor, userId, amount):
        """
        Credit amount to a user's wallet within an active transaction.
        Locks the wallet row first to avoid lost updates from concurrent trades.
        """
        if cursor is None:
            raise ValueError("Cursor cannot be None")
        if userId is None or userId <= 0:
            raise ValueError("User ID must be a positive integer")
        if amount is None or amount <= 0:
            raise ValueError("Credit amount must be positive")

        wallet = self.getWalletBalanceWithLock(cursor, userId)
        if wallet is None:
            raise Exception(f"No wallet found for user {userId}")

        new_balance = Decimal(str(wallet["balance"])) + Decimal(str(amount))
        cursor.execute(
            QueryLoader.get('wallet.yaml', 'update_wallet_balance'),
            (new_balance, userId)
        )
        return new_balance
