from fastapi import APIRouter, Depends

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
