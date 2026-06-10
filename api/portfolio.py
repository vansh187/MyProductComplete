from fastapi import APIRouter,Depends
from pydantic import BaseModel
from utils.auth_dependency import get_current_user
from service.portfolioService import portfolioService
from datetime import datetime
router=APIRouter()

@router.get("/getPortfolioForLoggedInUser")
def getPortFolioforLoggedInUser(current_user=Depends(get_current_user)):
    userId = current_user.get('user_id')
    if not userId:
        return {"success": False, "message": "User ID not found"}
    portfolio= portfolioService.getPortfolioServiceforLoggedInUser(userId)
    if portfolio is None:
        return{
            "userId":userId,
            "message": "No portfolio found for User"
        }  
    else:
         return{
             "generated_at": datetime.now(),
             "success" : True,
            "userId":userId,
            "total_positions": len(portfolio),
            "portfolio":portfolio
        } 


@router.get("/getPortfolioOfLoggedInUserWithProfitLoss")
def getPortfolioOfLoggedInUserWithProfitLoss(currentUser=Depends(get_current_user)):
    try:
        userId = currentUser.get('user_id')
        if not userId:
            return {"success": False, "message": "User ID not found"}
        
        rawData = portfolioService.getPortfolioOfLoggedInUserWithProfitLoss(userId)
        
        if rawData is None:
            return {
                "success": False,
                "message": "No portfolio data found for user"
            }
        
        if not rawData.holdings:
            rawData.holdings = []
        
        return {
            "success": True,
            "user_id": rawData.user_id,
            "total_pnl": rawData.total_pnl,
            "portfolio": [
                {
                    "symbol": h.symbol,
                    "quantity": h.quantity,
                    "avg_price": h.avg_price,
                    "current_price": h.current_price,
                    "pnl": h.pnl
                }
                for h in rawData.holdings
            ]
        }
    except (AttributeError, KeyError, TypeError) as e:
        return {"success": False, "message": f"Error building portfolio and loss: {str(e)}"}
    except Exception as e:
        return {"success": False, "message": "An unexpected error occurred while fetching portfolio"}