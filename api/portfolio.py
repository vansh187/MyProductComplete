from fastapi import APIRouter,Depends, Query
from pydantic import BaseModel
from utils.auth_dependency import get_current_user
from service.portfolioService import portfolioService
from utils.assetBuckets import ASSET_TYPE_BUCKETS
from datetime import datetime
router=APIRouter()

@router.get("/getPortfolioForLoggedInUser")
def getPortFolioforLoggedInUser(current_user=Depends(get_current_user)):
    userId = current_user.get('user_id')
    if not userId:
        return {"success": False, "message": "User ID not found"}
    portfolio= portfolioService().getPortfolioServiceforLoggedInUser(userId)
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
def getPortfolioOfLoggedInUserWithProfitLoss(
    bucket: str = Query(None, description="STOCKS or FNO; omit for all holdings"),
    currentUser=Depends(get_current_user)
):
    try:
        userId = currentUser.get('user_id')
        if not userId:
            return {"success": False, "message": "User ID not found"}

        if bucket == "FNO":
            return {
                "success": False,
                "message": "F&O positions are tracked separately - use GET /getFnoPositionsForLoggedInUser "
                           "or GET /getAssetClassSummary?bucket=FNO instead"
            }
        if bucket is not None and bucket not in ASSET_TYPE_BUCKETS:
            return {"success": False, "message": f"Invalid bucket: {bucket}"}

        rawData = portfolioService().getPortfolioOfLoggedInUserWithProfitLoss(userId, bucket)

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
                    "pnl": h.pnl,
                    "asset_type": h.asset_type
                }
                for h in rawData.holdings
            ]
        }
    except ValueError as e:
        return {"success": False, "message": str(e)}
    except (AttributeError, KeyError, TypeError) as e:
        return {"success": False, "message": f"Error building portfolio and loss: {str(e)}"}
    except Exception as e:
        return {"success": False, "message": "An unexpected error occurred while fetching portfolio"}