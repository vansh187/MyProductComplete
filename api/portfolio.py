from fastapi import APIRouter,Depends
from pydantic import BaseModel
from utils.auth_dependency import get_current_user
from service.portfolioService import portfolioService

router=APIRouter()

@router.get("/getPortfolioForLoggedInUser")
def getPortFolioforLoggedInUser(current_user=Depends(get_current_user)):
    userId=current_user['user_id']
    portfolio= portfolioService.getPortfolioServiceforLoggedInUser(userId)
    if portfolio is None:
        return{
            "userId":userId,
            "message": "No portfolio found for User"
        }  
    else:
         return{
            "userId":userId,
            "portfolio":portfolio
        } 

