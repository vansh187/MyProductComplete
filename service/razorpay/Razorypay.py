
from service.razorpay.RazorPayMangerService import RazorPayManagerService
from fastapi import BackgroundTasks as background_tasks
import os
from dotenv import load_dotenv
import hmac
import hashlib
load_dotenv()
class Razorpay:
    def __init__(self):
        pass
    
    
    
    def invokeRazorPayintegration(self,walletLedger,userId,background_tasks):
        razorPayManagerService=RazorPayManagerService()
        razorPaystatus=razorPayManagerService.invokeRazorPayServiceForAddFundsToAccount(walletLedger,userId,background_tasks)
        if razorPaystatus is None:
            return{
                "Message" :"Invalid Request"
                
            }
        return razorPaystatus
        """ elif razorPaystatus.status=="failure":
            return razorPaystatus
        elif razorPaystatus.status =="success":
               return razorPaystatus
                """
            
    def verifyPaymentSignature(self,payload,userId,background_tasks):
        razorPayManagerService=RazorPayManagerService()
        secret = os.getenv("RAZORPAY_SECRET_KEY")
        razorpay_order_id=payload.razorpay_order_id
        
        is_authentic = razorPayManagerService.verify_payment_signature(
            razorpay_order_id=razorpay_order_id,
            razorpay_payment_id=payload.razorpay_payment_id,
            razorpay_signature=payload.razorpay_signature,
            userId=userId,
            background_tasks=background_tasks
        )
        
        return is_authentic

    
    def verifyWebhookSignature(self,raw_body,webhook_signature):
        razorPayManger=RazorPayManagerService()
        print("inside verifyWebhookSignature inside RazorPay")
        return  razorPayManger.verify_webhook_signature(raw_body,webhook_signature)
    
    
    def verifyPaymentSignatureWebhook(self,payment_entity,user_id):
         razorPayManagerService=RazorPayManagerService()
         secret = os.getenv("RAZORPAY_SECRET_KEY")
         razorpay_order_id=payment_entity.razorpay_order_id
         is_authentic = razorPayManagerService.verify_webhook_signature(
            payload=payment_entity,
            razorpay_signature=razorpay_order_id,
            userId=user_id 
        )
    
    
    
    def verifyPaymentSignatureWebHook(self, payload, userId):
        razorPayManagerService = RazorPayManagerService()
        print("inside verifyPaymentSignatureWebHook after backgroundprocess")
        razorpay_order_id = payload.get("order_id")
        razorpay_payment_id = payload.get("id")
        if not razorpay_order_id:
            print(f"Webhook: order_id is null for payment {razorpay_payment_id}, skipping DB update")
            return None
        is_authentic = razorPayManagerService.invokeCallToDatabase(
            razorpay_order_id=razorpay_order_id,
            razorpay_payment_id=razorpay_payment_id,
            userId=userId,
        )
        print("All authentication is in process")
        return is_authentic