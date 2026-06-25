from database.PostgresConnectionFactory import PostgresConnectionFactory
from utils.query_loader import QueryLoader
import psycopg2.extras
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

    def updatePaymentStatus(self, razorpay_order_id, razorpay_payment_id, userId):
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
            conn.commit()
            print("Transaction update is success")
        except Exception as ex:
            if conn:
                conn.rollback()
            print(f"Error in updating status: {str(ex)}")
        finally:
            if conn is not None:
                conn.close()
            if cursor is not None:
                cursor.close()

    def insertUpdateWallet(self, userId, razorpay_order_id):
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
            if walletRecord["wallet_id"] is None:
                cursor.execute(
                    QueryLoader.get('razorpay.yaml', 'insert_wallet'),
                    (userId, walletRecord["transaction_amount"])
                )
                conn.commit()
                print("insert wallet record with new funds")
            else:
                newWalletBalance = walletRecord["balance"] + walletRecord["transaction_amount"]
                cursor.execute(
                    QueryLoader.get('razorpay.yaml', 'update_wallet_balance'),
                    (newWalletBalance, userId)
                )
                conn.commit()
                print("Transaction update is success")
        except Exception as ex:
            if conn:
                conn.rollback()
            print(f"Error in updating wallet: {str(ex)}")
        finally:
            if conn is not None:
                conn.close()
            if cursor is not None:
                cursor.close()

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
