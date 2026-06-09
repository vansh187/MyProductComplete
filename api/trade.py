from typing import Optional
from utils.auth_dependency import get_current_user
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from service.tradeHistoryService import TradeHistoryService as tradeHistory

router = APIRouter()

@router.get("/trades")
def getTrades(current_user=Depends(get_current_user)):
    userId=current_user["user_id"]
    tradeOrders= tradeHistory.getTradeOrdersById(userId)
    if tradeOrders is None:
        return{
            "User": current_user,
            "Message" : "No Trade Orders Found"

        }
    else :
        return{
            "User": current_user,
            "tradeOrder": tradeOrders
        }
