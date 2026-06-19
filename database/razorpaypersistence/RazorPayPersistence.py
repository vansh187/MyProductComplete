from database.ConnectionFactory import ConnectionFactory
import os
from dotenv import load_dotenv
from enum import Enum

load_dotenv()
class RazorPayPersistence:
    def __init__(self,walletLedger=None,razorPayOrder=None,userId=None):
        self.walletLedger=walletLedger
        self.razorPayOrder=razorPayOrder
        self.userId=userId
    

    
    
    def inssertPendingstatusOfAddFunds(self, walletLedger,razonrPayOrder,userId):
        conn = None
        cursor = None
        INSERT_RAZORPAY_LEDGER = """INSERT INTO wallet_ledger (
                                    user_id, 
                                    razorpay_order_id, 
                                    razorpay_payment_id, 
                                    amount, 
                                    currency, 
                                    transaction_type, 
                                    status, 
                                    created_at
                                ) VALUES (
                                    %s,                         
                                     %s,     
                                    NULL,                       
                                     %s,                      
                                     %s,                   
                                     %s,          
                                     %s,                 
                                    NOW()                       
                                )"""
        
        try:
            conn = ConnectionFactory.create_connection(os.getenv("MYSQLHOST"),
                     os.getenv("MYSQLUSER"),
                     os.getenv("MYSQLPASSWORD"),
                     os.getenv("MYSQLDATABASE"),
                     os.getenv("MYSQLPORT", 3306))
            cursor = conn.cursor()
            cursor.execute(INSERT_RAZORPAY_LEDGER,(userId,razonrPayOrder.get("id"),
                                                 razonrPayOrder.get("amount"), razonrPayOrder.get("currency"),
                                                  TransactionType.WALLET_FUNDING,TransactionStatus.PENDING
                                                   ))
            conn.commit()
            print("Insert Success")
        
        except Exception as ex:
            raise Exception("Error in inserting into wallet Ledger"+ex)    
        
        finally:
            if conn is not None:
                conn.close()
            
            if cursor is not None:
                cursor.close()    

    
    
    def updatePaymentStatus(self,razorpay_order_id,razorpay_payment_id,userId):
        conn=None
        cursor=None
        UPDATE_PAYMENT_STATUS="""UPDATE wallet_ledger 
                                SET status = %s, 
                                    razorpay_payment_id = %s 
                                WHERE user_id = %s 
                                AND razorpay_order_id = %s 
                                AND status = %s"""
        try:
           conn= ConnectionFactory.create_connection(os.getenv("MYSQLHOST"),
                     os.getenv("MYSQLUSER"),
                     os.getenv("MYSQLPASSWORD"),
                     os.getenv("MYSQLDATABASE"),
                     os.getenv("MYSQLPORT", 3306) )
           cursor=conn.cursor()
           cursor.execute(UPDATE_PAYMENT_STATUS,(TransactionStatus.SUCCESS,razorpay_payment_id,userId,razorpay_order_id,TransactionStatus.PENDING))
           conn.commit()
           print("Transaction update is success")
        except Exception as ex:
           print("Error in updating status"+ex)     

        finally:
            if conn is not None:
                conn.close()
            if cursor is not None:
                cursor.close()    

    def insertUpdateWallet(self,userId,razorpay_order_id):
        conn=None
        cursor=None
        newWalletBalance=0.0
        UPDATE_WALLET_STATEMENT=""" UPDATE wallets 
                                    SET balance = %s 
                                    WHERE user_id = %s; """
                                
        SELECT_WALLET_STATEMENT="""
                                      SELECT 
                                        w.wallet_id,
                                        -- If the wallet doesn't exist yet, return 0.00 instead of NULL
                                        COALESCE(w.balance, 0.00) AS balance, 
                                        wl.amount AS transaction_amount,
                                        wl.status AS transaction_status
                                    FROM wallets w
                                    RIGHT JOIN wallet_ledger wl ON w.user_id = wl.user_id
                                    WHERE wl.razorpay_order_id = %s
                                    AND wl.user_id = %s

                                """
        INSERT_WALLET_STATEMENT="""INSERT INTO wallets (user_id, balance) 
                                    VALUES (%s, %s);"""
        
        try:
           conn= ConnectionFactory.create_connection(os.getenv("MYSQLHOST"),
                     os.getenv("MYSQLUSER"),
                     os.getenv("MYSQLPASSWORD"),
                     os.getenv("MYSQLDATABASE"),
                     os.getenv("MYSQLPORT", 3306) )
           cursor=conn.cursor(dictionary=True)
           cursor.execute(SELECT_WALLET_STATEMENT,(razorpay_order_id,userId))
           walletRecord=cursor.fetchone()
           if walletRecord["wallet_id"] is None:
               cursor.execute(INSERT_WALLET_STATEMENT,(userId,walletRecord["transaction_amount"])) 
               conn.commit()
               print("insert wallet record with new funds")
           else:    
                    oldBalance=walletRecord["balance"]
                    amountToAdd=walletRecord["transaction_amount"]
                    newWalletBalance=oldBalance+amountToAdd
                    cursor.execute(UPDATE_WALLET_STATEMENT,(newWalletBalance,userId))
                    conn.commit()
                    print("Transaction update is success")
        except Exception as ex:
           print("Error in updating status"+ex)     

        finally:
            if conn is not None:
                conn.close()
            if cursor is not None:
                cursor.close()    
    
    
    
class TransactionType(str,Enum):
    WALLET_FUNDING = "1"
    WITHDRAWAL = "2"
    ASSET_PURCHASE = "3"
    ASSET_SALE = "4"

class TransactionStatus(str,Enum):
    PENDING = "1"
    SUCCESS = "2"
    FAILED = "3"           