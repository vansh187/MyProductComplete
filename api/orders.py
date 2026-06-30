from typing import Optional, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from utils.auth_dependency import get_current_user
from service.orderService import OrderService
from service.portfolioService import portfolioService
from service.executionEngine import ExecutionEngine
from service.walletbalance.WalletBalanceService import WalletBalanceService
from decimal import Decimal

router = APIRouter()

class OrderCreate(BaseModel):
    id: Optional[int] = None
    symbol: str
    side: Literal["BUY", "SELL"]
    quantity: int
    price: Decimal
    status: str
    remainingQty: Optional[int] = None

@router.get("/orders")
def get_orders(current_user=Depends(get_current_user)):
    user_id = current_user["user_id"]
    orderSerive=OrderService()
    orders = orderSerive.getorders(user_id)
    return {            "Message": "Orders retrieved successfully",
                "User": current_user,
                "Orders": orders
            }


@router.post("/orders")
def create_order(order: OrderCreate, current_user=Depends(get_current_user)):
    user_id = current_user["user_id"]

    if order.side == "BUY":
        required = Decimal(str(order.quantity)) * order.price
        wallet = WalletBalanceService().getWalletBalance(user_id)
        balance = Decimal(str(wallet["balance"])) if wallet and wallet.get("balance") is not None else Decimal("0")
        if balance < required:
            raise HTTPException(
                status_code=400,
                detail=f"Insufficient balance. Required: ₹{required:.2f}, Available: ₹{balance:.2f}"
            )

    orderSerive = OrderService()
    id = orderSerive.create_order(order, user_id)
    order.id = id
    executionEngine = ExecutionEngine(order)
    status = executionEngine.executeOrder(order, user_id)
    return status


@router.get("/getOrderById/{orderId}")
def getOrderById(orderId:int,current_user=Depends(get_current_user)):
    userId = current_user["user_id"]
    orderSerive=OrderService()
    order = orderSerive.getOrderById(userId, orderId)
    return {
                "Message": "Order retrieved successfully",
                "User": current_user,
                "Order": order
            }


@router.get("/cancelOrderById/{orderId}")
def cancelOrderById(orderId:int,current_user=Depends(get_current_user)):
    userId = current_user["user_id"]
    orderSerive=OrderService()
    order = orderSerive.cancelOrderById(userId, orderId)
    if order is None:
        return{
                "Message": "Only pending orders are allowed",
                "User": current_user,
        }
    else:
        return {
                "Message": "Order cancelled Success",
                "User": current_user,
                "Order": order
            }
