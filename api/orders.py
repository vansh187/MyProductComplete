from fastapi import APIRouter, Depends
from pydantic import BaseModel
from utils.auth_dependency import get_current_user
from service.orderService import create_order as service_create_order
from service.orderService import getorders as service_get_orders
from service.orderService import getOrderById as service_get_OrderById
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
    service_create_order(order, user_id)

    return {
                "Message": "Order created successfully",
                "User": current_user
            }


class OrderCreate(BaseModel):
    
    symbol: str
    side: str
    quantity: int
    price: float
    status: str


@router.get("/getOrderById/{orderId}")
def getOrderById(orderId:int,current_user=Depends(get_current_user)):
    userId = current_user["user_id"]
    order = service_get_OrderById(userId, orderId)
    return {
                "Message": "Order retrieved successfully",
                "User": current_user,
                "Order": order
            }

