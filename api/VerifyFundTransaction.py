from fastapi import APIRouter,Depends,BackgroundTasks
from utils.auth_dependency import get_current_user
from service.razorpay.Razorypay import Razorpay
from pydantic import BaseModel

router=APIRouter()

@router.post("/v1/VerifyFundPayements")
def verifyFundPayments(payload:VeriFyTransaction,background_tasks: BackgroundTasks,currentUser=Depends(get_current_user)):
    userId=currentUser["user_id"]
    razorPay=Razorpay()
    isValid=razorPay.verifyPaymentSignature(payload,userId,background_tasks)
    if isValid:
        ## db call for for updating amount and status
        return{
            "status":200,
            "message" : "Payment Verified"
            
        }
    else:
        return{
            "message" : "Not Verified"    
        }
    
class VeriFyTransaction(BaseModel):
    razorpay_order_id: str=None
    razorpay_payment_id: str=None
    razorpay_signature: str=None