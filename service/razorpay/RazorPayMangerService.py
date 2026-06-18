import hmac
import hashlib
import razorpay
import os
from dotenv import load_dotenv
load_dotenv()

class RazorPayManagerService:

    def __init__(self):
        self.key_id = None
        self.key_secret = None
        self.client = None
    
    
    
    def invokeRazorPayServiceForAddFundsToAccount(self,walletLedger,userId):
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
                return {
                "status":"success", 
                "userId":userId,  
                "razorpay_order": razonrPayOrder,
                "amount_subunits": walletLedger.amount,
                "currency": walletLedger.currency
            }
        else:
            return {
                "status":"failure", 
                "Message":"Invalid razorpay api keys"
            }
    
    
    def verify_payment_signature(self, razorpay_order_id: str, razorpay_payment_id: str, razorpay_signature: str) -> bool:
        """
        Step 2: Cryptographically verifies the payment signature returned by the frontend checkout window.
        Returns True if authentic, False if compromised.
        """
        # Construct the expected raw text blob payload
        signature_payload = f"{razorpay_order_id}|{razorpay_payment_id}"
        
        # Generate local SHA256 HMAC hash using your secret key
        local_hash = hmac.new(
            bytes(self.key_secret, "utf-8"),
            bytes(signature_payload, "utf-8"),
            hashlib.sha256
        ).hexdigest()
        
        # Secure string comparison to prevent timing attacks
        return hmac.compare_digest(local_hash, razorpay_signature)    
        
        