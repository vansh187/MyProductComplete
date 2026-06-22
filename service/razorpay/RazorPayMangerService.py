import hmac
import hashlib
import razorpay
import os
from dotenv import load_dotenv
from database.razorpaypersistence.RazorPayPersistence import RazorPayPersistence
from fastapi import BackgroundTasks 
load_dotenv()

class RazorPayManagerService:

    def __init__(self):
        self.key_id = None
        self.key_secret = None
        self.client = None
        
    
    
    def invokeRazorPayServiceForAddFundsToAccount(self,walletLedger,userId,background_tasks):
        self.key_id=os.getenv("RAZORPAY_API_KEY")
        self.key_secret=os.getenv("RAZORPAY_SECRET_KEY")
        if self.key_id is not None and self.key_secret is not None:
            self.client=razorpay.Client(auth=(self.key_id,self.key_secret))
            print("client success")
            order_payload = {
            "amount": walletLedger.amount ,
            "currency": walletLedger.currency,
            "receipt": f"rcpt_{userId}_{int(walletLedger.amount)}",
            "notes": {
                "user_id": userId,
                "module": "wallet_funding"
            }
                
        }
            razonrPayOrder = self.client.order.create(data=order_payload)
            if razonrPayOrder is not None:
                razorPayPersistence=RazorPayPersistence(walletLedger,razonrPayOrder,userId)
                
                def run_background_insert():
                    razorPayPersistence.inssertPendingstatusOfAddFunds(
                        walletLedger, 
                        razonrPayOrder, 
                        userId
                    )
                
                background_tasks.add_task(
                   run_background_insert
                )
                
                return {
                "status":"success", 
                "userId":userId,  
                "razorpay_order": razonrPayOrder,
                "amount_subunits": walletLedger.amount,
                "currency": walletLedger.currency,
                "key": self.key_id
            }
        else:
            return {
                "status":"failure", 
                "Message":"Invalid razorpay api keys"
            }
    
    
    def verify_payment_signature(self, razorpay_order_id: str, razorpay_payment_id: str, razorpay_signature: str,userId:int,background_tasks: BackgroundTasks) -> bool:
        """
        Step 2: Cryptographically verifies the payment signature returned by the frontend checkout window.
        Returns True if authentic, False if compromised.
        """
        # Construct the expected raw text blob payload
        try:
                self.key_id=os.getenv("RAZORPAY_API_KEY")
                self.key_secret=os.getenv("RAZORPAY_SECRET_KEY")
                self.client=razorpay.Client(auth=(self.key_id,self.key_secret))
                signature_payload = f"{razorpay_order_id}|{razorpay_payment_id}"
                
                # Generate local SHA256 HMAC hash using your secret key
                valid_signature = hmac.new(
                    bytes(str(str(self.key_secret)), "utf-8"),
                    bytes(str(str(signature_payload)), "utf-8"),
                    hashlib.sha256
                    ).hexdigest()
                
                # Secure string comparison to prevent timing attacks
                params_dict = {
                    'razorpay_order_id': razorpay_order_id,
                    'razorpay_payment_id': razorpay_payment_id,
                    'razorpay_signature': valid_signature
                }
                self.client.utility.verify_payment_signature(params_dict)
                """razorPayPersistence=RazorPayPersistence()
                def run_background_insert():
                    razorPayPersistence.updatePaymentStatus(
                       razorpay_order_id , 
                       razorpay_payment_id , 
                        userId
                    )
                    
                    razorPayPersistence.insertUpdateWallet(userId,razorpay_order_id)
                
                background_tasks.add_task(
                   run_background_insert 
                )"""
                return True   
        except Exception as ex:
            print("Error in verification of payment"+ex)
    
    
    def verify_webhook_signature(self, raw_body, webhook_signature) -> bool:
        """
        Validates the incoming webhook signature using the raw request body bytes.
        """
        try:
            #This is the secret passphrase you will set in the Razorpay Dashboard
            webhook_secret = "WEBHOOK_9897"#os.getenv("RAZORPAY_WEBHOOK_SECRET")""
            if not webhook_secret:
                print("RAZORPAY_WEBHOOK_SECRET is not set in the environment variables.")
                return False
            
            # Using Razorpay's utility helper to verify the signature
            print("inside verify_webhook_signature inside RazorPayManagerService")
            print("webhook signatue"+webhook_signature)
            print(raw_body)
            self.key_id=os.getenv("RAZORPAY_API_KEY")
            self.key_secret=os.getenv("RAZORPAY_SECRET_KEY")
            self.client=razorpay.Client(auth=(self.key_id,self.key_secret))
            print("Client initiation done")
            self.client.utility.verify_webhook_signature(
                raw_body.decode('utf-8'), 
                webhook_signature, 
                webhook_secret
            )
            print("inside verify_webhook_signature after utility inside RazorPayManagerService")
            return True
        except Exception as e:
            print(f" Webhook signature verification failed: {e}")
            return False   
    
    
    
    def verify_payment_signatureFromWebHook(self, razorpay_order_id: str, razorpay_payment_id: str, razorpay_signature: str,userId:int) -> bool:
        """
        Step 2: Cryptographically verifies the payment signature returned by the frontend checkout window.
        Returns True if authentic, False if compromised.
        """
        # Construct the expected raw text blob payload
        try:
                self.key_id=os.getenv("RAZORPAY_API_KEY")
                self.key_secret=os.getenv("RAZORPAY_SECRET_KEY")
                self.client=razorpay.Client(auth=(self.key_id,self.key_secret))
                signature_payload = f"{razorpay_order_id}|{razorpay_payment_id}"
                
                # Generate local SHA256 HMAC hash using your secret key
                valid_signature = hmac.new(
                    bytes(str(str(self.key_secret)), "utf-8"),
                    bytes(str(str(signature_payload)), "utf-8"),
                    hashlib.sha256
                    ).hexdigest()
                
                # Secure string comparison to prevent timing attacks
                params_dict = {
                    'razorpay_order_id': razorpay_order_id,
                    'razorpay_payment_id': razorpay_payment_id,
                    'razorpay_signature': valid_signature
                }
                self.client.utility.verify_payment_signature(params_dict)
                razorPayPersistence=RazorPayPersistence()
                
                razorPayPersistence.updatePaymentStatus(
                       razorpay_order_id , 
                       razorpay_payment_id , 
                        userId
                    )
                    
                razorPayPersistence.insertUpdateWallet(userId,razorpay_order_id)
                
                return True   
        except Exception as ex:
            print("Error in verification of payment"+ex)    
            
    def invokeCallToDatabase(self,razorpay_order_id,razorpay_payment_id,userId):
        print("calling to database")
        razorPayPersistence=RazorPayPersistence()
        razorPayPersistence.updatePaymentStatus(
                       razorpay_order_id , 
                       razorpay_payment_id , 
                        userId
                    )
                    
        razorPayPersistence.insertUpdateWallet(userId,razorpay_order_id) 
        print("database update completed")  