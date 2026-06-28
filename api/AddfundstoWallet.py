from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field, condecimal
from utils.auth_dependency import get_current_user
from fastapi import APIRouter
from decimal import Decimal
from service.razorpay.Razorypay import Razorpay
from service.walletbalance.WalletBalanceService import WalletBalanceService

router = APIRouter()

class WalletLedger(BaseModel):
    amount: Decimal = Decimal("0.0")
    currency: str = None

@router.post("/v1/addFundsToWallet")
def addFundsToWallet(walletLedger: WalletLedger, background_tasks: BackgroundTasks, current_user=Depends(get_current_user)):
    print("Adding funds")
    userId = current_user["user_id"]
    razorpay = Razorpay()
    return razorpay.invokeRazorPayintegration(walletLedger, userId, background_tasks=background_tasks)


@router.post("/v1/getWalletBalance")
def getWalletBalance(current_user=Depends(get_current_user)):
    try:
        userId = current_user["user_id"]
        walletBalanceService = WalletBalanceService()
        walletBalance = walletBalanceService.getWalletBalance(userId)
        if walletBalance is None:
            return {"user_id": userId, "balance": 0.0}
        return {"user_id": walletBalance["user_id"], "balance": walletBalance["balance"]}
    except Exception as ex:
        raise HTTPException(status_code=500, detail=f"Failed to fetch wallet balance: {str(ex)}")

