from fastapi import APIRouter, BackgroundTasks,Depends
from pydantic import BaseModel, Field, condecimal
from utils.auth_dependency import get_current_user
from fastapi import APIRouter
from decimal import Decimal
from service.razorpay.Razorypay import Razorpay 

router = APIRouter()

@router.post("/v1/addFundsToWallet")
def addFundsToWallet(walletLedger: WalletLedger, background_tasks: BackgroundTasks,current_user=Depends(get_current_user) ):
    print ("Adding funds")
    """msg = "order_T3DvWN54raoa0B|pay_MOCK_SUCCESS_12345"
    
    secret = "0odiqmdsQKqMnZ2lLSUos0fn" 
    test_signature = hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()
    print(f"Your Test Signature: {test_signature}")"""
    userId=current_user["user_id"]
    razorpay = Razorpay()
    return razorpay.invokeRazorPayintegration(walletLedger,userId,background_tasks= background_tasks)
    

class WalletLedger(BaseModel):
     amount:float=0.0
     currency: str=None      

