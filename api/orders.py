from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from utils.auth_dependency import get_current_user
from service.orderService import create_order as service_create_order
from service.orderService import getorders as service_get_orders
from service.orderService import getOrderById as service_get_OrderById
from service.orderService import cancelOrderById as service_cancelOrderById
from service.portfolioService import portfolioService
from service.executionEngine import ExecutionEngine
from decimal import Decimal
#from service.portfolioService import process_buy as service_process_sell
router = APIRouter()

@router.get("/orders")
def get_orders(current_user=Depends(get_current_user)):
    user_id = current_user["user_id"]
    orders = service_get_orders(user_id)
    return {            "Message": "Orders retrieved successfully", 
                "User": current_user,
                "Orders": orders
            }


@router.post("/orders")
def create_order(order: OrderCreate, current_user=Depends(get_current_user)):
    user_id = current_user["user_id"]
    id=service_create_order(order, user_id)
    order.id=id
    status=ExecutionEngine.executeOrder(order,user_id)
   ## if order.side == "BUY":
     ##   portfolioService.process_buyer(user_id, order.symbol, order.quantity, order.price)

##    elif order.side == "SELL":
  ##    portfolioService.process_seller(user_id, order.symbol, order.quantity,order.price)
   
    return status


class OrderCreate(BaseModel):
    
    id:Optional[int]=None
    symbol: str
    side: str
    quantity: int
    price: Decimal
    status: str
    remainiungQty:Optional[int]=None

@router.get("/getOrderById/{orderId}")
def getOrderById(orderId:int,current_user=Depends(get_current_user)):
    userId = current_user["user_id"]
    order = service_cancelOrderById(userId, orderId)
    return {
                "Message": "Order retrieved successfully",
                "User": current_user,
                "Order": order
            }


@router.get("/cancelOrderById/{orderId}")
def cancelOrderById(orderId:int,current_user=Depends(get_current_user)):
    userId = current_user["user_id"]
    order = service_cancelOrderById(userId, orderId)
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

