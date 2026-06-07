from fastapi import APIRouter, Depends
from pydantic import BaseModel
from utils.auth_dependency import get_current_user
from service.orderService import create_order as service_create_order
router = APIRouter()

@router.get("/orders")
def get_orders(current_user=Depends(get_current_user)):
    return {
                "Message": "protected route accessed",
                "User": current_user
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
