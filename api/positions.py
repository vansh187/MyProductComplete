from fastapi import APIRouter, Depends
from utils.auth_dependency import get_current_user
from service.positionsService import PositionsService

router = APIRouter()


@router.get("/getFnoPositionsForLoggedInUser")
def getFnoPositionsForLoggedInUser(currentUser=Depends(get_current_user)):
    try:
        userId = currentUser.get('user_id')
        if not userId:
            return {"success": False, "message": "User ID not found"}

        positions = PositionsService().getPositionsForUser(userId)

        return {
            "success": True,
            "userId": userId,
            "total_positions": len(positions),
            "positions": [
                {
                    "symbol": p["tsym"],
                    "underlying": p["underlying"],
                    "exchange": p["exchange"],
                    "expiry": p["expiry"],
                    "strike": p["strike"],
                    "option_type": p["option_type"],
                    "contract_type": p["contract_type"],
                    "lot_size": p["lot_size"],
                    "product_type": p["product_type"],
                    "netqty": p["netqty"],
                    "netavgprc": p["netavgprc"],
                    "buyqty": p["buyqty"],
                    "sellqty": p["sellqty"],
                    "buyavgprc": p["buyavgprc"],
                    "sellavgprc": p["sellavgprc"],
                    "realized_pnl": p["realized_pnl"],
                    "status": p["status"],
                }
                for p in positions
            ]
        }
    except (AttributeError, KeyError, TypeError) as e:
        return {"success": False, "message": f"Error building F&O positions: {str(e)}"}
    except Exception:
        return {"success": False, "message": "An unexpected error occurred while fetching F&O positions"}

from utils.auth_dependency import get_current_user
from database.positionCache import PositionCache

router = APIRouter()
_position_cache = PositionCache()


@router.get("/getPositionsForLoggedInUser")
def getPositionsForLoggedInUser(current_user=Depends(get_current_user)):
    """Serves a user's OPEN positions straight from the Redis cache, kept
    live-updated on every tick (see service/positionTickService.py) - closed
    positions are Postgres-only history and aren't returned here."""
    user_id = current_user.get('user_id')
    if not user_id:
        return {"success": False, "message": "User ID not found"}

    positions = _position_cache.get_all_positions(user_id)

    return {
        "success": True,
        "user_id": user_id,
        "total_positions": len(positions),
        "positions": positions,
    }
