from database.PostgresConnectionFactory import PostgresConnectionFactory
from utils.query_loader import QueryLoader
import psycopg2.extras
from decimal import Decimal
from dotenv import load_dotenv
from enum import Enum

load_dotenv()


class RazorPayPersistence:

    def __init__(self, walletLedger=None, razorPayOrder=None, userId=None):
        self.walletLedger = walletLedger
        self.razorPayOrder = razorPayOrder
        self.userId = userId

    def inssertPendingstatusOfAddFunds(self, walletLedger, razonrPayOrder, userId):
        conn = None
        cursor = None
        try:
            conn = PostgresConnectionFactory.create_connection()
            cursor = conn.cursor()
            cursor.execute(
                QueryLoader.get('razorpay.yaml', 'insert_wallet_ledger'),
                (userId, razonrPayOrder.get("id"), walletLedger.amount,
                 razonrPayOrder.get("currency"),
                 TransactionType.WALLET_FUNDING, TransactionStatus.PENDING)
            )
            conn.commit()
            print("Insert Success")
        except Exception as ex:
            if conn:
                conn.rollback()
            raise Exception(f"Error in inserting into wallet Ledger: {str(ex)}")
        finally:
            if conn is not None:
                conn.close()
            if cursor is not None:
                cursor.close()

    def updatePaymentStatus(self, razorpay_order_id, razorpay_payment_id, userId) -> bool:
        """
        Atomically flips the ledger row from PENDING to SUCCESS.

        Returns True only if this call performed the PENDING->SUCCESS
        transition (i.e. this is the first time this payment has been
        processed). Returns False if the row was already SUCCESS (a
        duplicate/retried webhook delivery) or not found, so callers can use
        this as a single-use gate before crediting the wallet.
        """
        conn = None
        cursor = None
        try:
            conn = PostgresConnectionFactory.create_connection()
            cursor = conn.cursor()
            cursor.execute(
                QueryLoader.get('razorpay.yaml', 'update_payment_status'),
                (TransactionStatus.SUCCESS, razorpay_payment_id,
                 userId, razorpay_order_id, TransactionStatus.PENDING)
            )
            was_pending = cursor.rowcount > 0
            conn.commit()
            if was_pending:
                print("Transaction update is success")
            else:
                print(f"Payment for order {razorpay_order_id}/user {userId} was already processed - skipping duplicate")
            return was_pending
        except Exception as ex:
            if conn:
                conn.rollback()
            print(f"Error in updating status: {str(ex)}")
            return False
        finally:
            if conn is not None:
                conn.close()
            if cursor is not None:
                cursor.close()

    def insertUpdateWallet(self, userId, razorpay_order_id):
        """
        Credits a wallet for a confirmed Razorpay payment.

        Previously this read the current balance, added the transaction
        amount in Python, then wrote the total back in a separate
        unlocked UPDATE - a classic lost-update race: two concurrent
        credits for the same user (a retried webhook alongside a trade
        settlement credit, or two deposits landing close together) could
        both read the same stale balance and the second write would
        silently clobber the first, losing real money with no error.

        Fixed by never computing the new balance in application code:
        the existing-wallet path uses a single atomic
        `balance = balance + %s` UPDATE (Postgres serializes this at the
        row level with no read step to go stale), and the brand-new-wallet
        path takes a `pg_advisory_xact_lock` keyed on userId before
        deciding whether to INSERT, since a not-yet-existing row can't be
        protected by a row lock - this closes the one remaining race
        (two concurrent first-ever deposits for the same new user).
        """
        conn = None
        cursor = None
        try:
            conn = PostgresConnectionFactory.create_connection()
            cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cursor.execute(
                QueryLoader.get('razorpay.yaml', 'select_wallet_for_update'),
                (razorpay_order_id, userId)
            )
            walletRecord = cursor.fetchone()
            if walletRecord is None:
                print(f"No wallet_ledger record found for order {razorpay_order_id}, skipping wallet update")
                return

            transaction_amount = Decimal(str(walletRecord["transaction_amount"]))
            print(f"DEBUG: userId={userId}, transaction_amount={transaction_amount}")

            if walletRecord["wallet_id"] is None:
                # No wallet row exists yet - serialize concurrent first-time
                # credits for this user (a row lock can't protect a row
                # that doesn't exist yet), then re-check under the lock
                # before deciding to INSERT vs increment.
                cursor.execute("SELECT pg_advisory_xact_lock(%s)", (userId,))
                cursor.execute(
                    QueryLoader.get('wallet.yaml', 'get_wallet_balance_for_update'),
                    (userId,)
                )
                existing = cursor.fetchone()
                if existing is None:
                    cursor.execute(
                        QueryLoader.get('razorpay.yaml', 'insert_wallet'),
                        (userId, transaction_amount)
                    )
                    print(f"insert wallet record with new funds: balance={transaction_amount}")
                else:
                    cursor.execute(
                        QueryLoader.get('wallet.yaml', 'increment_wallet_balance'),
                        (transaction_amount, userId)
                    )
                    print(f"Wallet was created concurrently before lock acquired; credited {transaction_amount}")
            else:
                # Wallet already exists - atomic increment, no separate
                # read+write, so no lost-update race even without an
                # explicit application-level lock.
                cursor.execute(
                    QueryLoader.get('wallet.yaml', 'increment_wallet_balance'),
                    (transaction_amount, userId)
                )
                print(f"Wallet credited atomically: +{transaction_amount} for user {userId}")

            conn.commit()
        except Exception as ex:
            if conn:
                conn.rollback()
            print(f"Error in updating wallet: {str(ex)}")
        finally:
            if cursor is not None:
                cursor.close()
            if conn is not None:
                conn.close()

    def get_user_id_by_order_id(self, razorpay_order_id):
        conn = None
        cursor = None
        try:
            conn = PostgresConnectionFactory.create_connection()
            cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cursor.execute(
                QueryLoader.get('razorpay.yaml', 'get_user_id_by_order'),
                (razorpay_order_id,)
            )
            row = cursor.fetchone()
            return row["user_id"] if row else None
        except Exception as ex:
            print(f"Error fetching user_id by order_id: {str(ex)}")
            return None
        finally:
            if conn is not None:
                conn.close()
            if cursor is not None:
                cursor.close()


class TransactionType(str, Enum):
    WALLET_FUNDING = "1"
    WITHDRAWAL = "2"
    ASSET_PURCHASE = "3"
    ASSET_SALE = "4"


class TransactionStatus(str, Enum):
    PENDING = "1"
    SUCCESS = "2"
    FAILED = "3"
