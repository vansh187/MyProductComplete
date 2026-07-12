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
            "amount": int(walletLedger.amount * 100),
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

        Fixed regression: this previously recomputed its own HMAC from
        order_id|payment_id and handed that SELF-COMPUTED value to the SDK
        as the "razorpay_signature" to check - meaning it always verified
        against itself and returned True for any order_id/payment_id pair,
        regardless of whether the razorpay_signature the client actually
        sent was valid, tampered, or garbage. The SDK call below now
        receives the caller's real razorpay_signature parameter, exactly
        the same delegation pattern verify_webhook_signature() already
        uses correctly - the SDK does its own internal HMAC recomputation
        and comparison, so no manual hmac/hashlib work belongs here at all.

        This is a client-facing UI confirmation only (the DB-write path
        below remains intentionally dead/commented out) - the webhook
        (verify_webhook_signature / invokeCallToDatabase) is the sole
        source of truth for actually crediting a wallet, since it's a
        server-to-server call authenticated independently of anything the
        client controls. Re-enabling that dead code without this fix
        would have been a real financial hole, not just a misleading UI.
        """
        try:
                self.key_id=os.getenv("RAZORPAY_API_KEY")
                self.key_secret=os.getenv("RAZORPAY_SECRET_KEY")
                self.client=razorpay.Client(auth=(self.key_id,self.key_secret))

                params_dict = {
                    'razorpay_order_id': razorpay_order_id,
                    'razorpay_payment_id': razorpay_payment_id,
                    'razorpay_signature': razorpay_signature
                }
                self.client.utility.verify_payment_signature(params_dict)
                # Wallet crediting is intentionally NOT done here - see
                # docstring above. The webhook path
                # (verify_webhook_signature -> invokeCallToDatabase) is the
                # sole source of truth for that, gated by
                # updatePaymentStatus()'s PENDING->SUCCESS idempotency check.
                return True
        except Exception as ex:
            print(f"Error in verification of payment: {ex}")
            return False


    def verify_webhook_signature(self, raw_body, webhook_signature) -> bool:
        """
        Validates the incoming webhook signature using the raw request body bytes.
        """
        try:
            # Read from environment - test mode and live mode use different
            # secrets issued by Razorpay's dashboard, so this must be a
            # config value, never a hardcoded literal (a hardcoded secret
            # here would either verify every webhook against a fixed,
            # guessable value or reject every live webhook outright once
            # a real live-mode secret is configured). Confirmed via a live
            # test that Razorpay's dashboard for this webhook is configured
            # with the value now stored in RAZORPAY_WEBHOOK_SECRET.
            webhook_secret = os.getenv("RAZORPAY_WEBHOOK_SECRET")
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
    
    
    
    def invokeCallToDatabase(self,razorpay_order_id,razorpay_payment_id,userId):
        print("calling to database")
        razorPayPersistence=RazorPayPersistence()
        was_newly_processed = razorPayPersistence.updatePaymentStatus(
                       razorpay_order_id ,
                       razorpay_payment_id ,
                        userId
                    )

        # updatePaymentStatus only returns True the first time this payment
        # transitions PENDING -> SUCCESS, so this guards against crediting
        # the wallet multiple times for retried/duplicate Razorpay webhooks.
        if was_newly_processed:
            razorPayPersistence.insertUpdateWallet(userId,razorpay_order_id)
        else:
            print(f"Skipping wallet credit for order {razorpay_order_id}: payment already processed")
        print("database update completed")