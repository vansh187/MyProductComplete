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
            return {"user_id": userId, "balance": 0.0, "status": "wallet_not_initialized"}
        return {
            "user_id": walletBalance["user_id"],
            "balance": float(walletBalance["balance"]),
            "status": "success"
        }
    except Exception as ex:
        raise HTTPException(status_code=500, detail=f"Failed to fetch wallet balance: {str(ex)}")


@router.post("/v1/getWalletBalanceWithBreakdown")
def getWalletBalanceWithBreakdown(current_user=Depends(get_current_user)):
    """
    Get wallet balance with detailed breakdown of all transactions
    Useful for debugging wallet calculation issues
    """
    try:
        userId = current_user["user_id"]
        walletBalanceService = WalletBalanceService()
        walletBalance = walletBalanceService.getWalletBalance(userId)

        if walletBalance is None:
            return {
                "user_id": userId,
                "balance": 0.0,
                "status": "wallet_not_initialized",
                "transactions": []
            }

        # Get transaction history
        from database.PostgresConnectionFactory import PostgresConnectionFactory
        import psycopg2.extras

        conn = None
        cursor = None
        try:
            conn = PostgresConnectionFactory.create_connection()
            cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cursor.execute("""
                SELECT
                    user_id,
                    razorpay_order_id,
                    amount,
                    status,
                    created_at
                FROM wallet_ledger
                WHERE user_id::integer = %s
                ORDER BY created_at DESC
                LIMIT 20
            """, (userId,))
            transactions = cursor.fetchall()
            transaction_list = [
                {
                    "order_id": t["razorpay_order_id"],
                    "amount": float(t["amount"]),
                    "status": t["status"],
                    "created_at": str(t["created_at"])
                }
                for t in transactions
            ] if transactions else []

            return {
                "user_id": walletBalance["user_id"],
                "balance": float(walletBalance["balance"]),
                "status": "success",
                "transactions": transaction_list
            }
        finally:
            if cursor:
                cursor.close()
            if conn:
                conn.close()

    except Exception as ex:
        raise HTTPException(status_code=500, detail=f"Failed to fetch wallet balance: {str(ex)}")

