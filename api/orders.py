from fastapi import APIRouter, Depends
from pydantic import BaseModel
from utils.auth_dependency import get_current_user
router = APIRouter()

@router.get("/orders")
def get_orders(current_user=Depends(get_current_user)):
    return {
                "Message": "protected route accessed",
                "User": current_user
            }