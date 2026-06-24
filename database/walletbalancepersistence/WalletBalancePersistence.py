from database.ConnectionFactory import ConnectionFactory
import os
from dotenv import load_dotenv

load_dotenv()

class WalletBalancePersistence:

    def __init__(self):
        pass

    def getWalletBalance(self, userId):
        conn = None
        cursor = None
        SELECT_WALLET_BALANCE = """SELECT user_id, balance FROM wallets WHERE user_id = %s"""
        try:
            conn = ConnectionFactory.create_connection(
                os.getenv("MYSQLHOST"),
                os.getenv("MYSQLUSER"),
                os.getenv("MYSQLPASSWORD"),
                os.getenv("MYSQLDATABASE"),
                os.getenv("MYSQLPORT", 3306)
            )
            if conn is None:
                raise Exception("Database connection could not be established")
            cursor = conn.cursor(dictionary=True)
            cursor.execute(SELECT_WALLET_BALANCE, (userId,))
            row = cursor.fetchone()
            return row
        except Exception as ex:
            raise Exception(f"Exception while fetching wallet balance: {str(ex)}")
        finally:
            if conn is not None:
                conn.close()
            if cursor is not None:
                cursor.close()
