
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
               