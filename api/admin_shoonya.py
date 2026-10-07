import asyncio
import logging
from datetime import datetime

from fastapi import APIRouter, Request, HTTPException, Depends
from pydantic import BaseModel
from utils.market_hours import IST_OFFSET
from utils.auth_dependency import get_current_user, require_admin

# Every route here controls or exposes the master broker account (OAuth
# login, the whole platform's real position book). Open to any logged-in
# user while ADMIN_USER_IDS is empty; set it to restrict to those ids - see
# require_admin.
router = APIRouter(prefix="/admin/shoonya", tags=["Shoonya Admin"], dependencies=[Depends(require_admin)])
logger = logging.getLogger("admin_shoonya")


class CodeExchangeRequest(BaseModel):
    code: str


@router.get("/auth-url")
def get_auth_url(request: Request, current_user=Depends(get_current_user)):
    """
    Step 1 of OAuth flow.
    Returns the browser URL to open for Shoonya authentication.
    """
    from marketengine.ShoonyaConnection import get_or_create_connection
    shoonya = get_or_create_connection(request.app)
    return {
        "oauth_url": shoonya.get_oauth_url(),
        "instructions": (
            "1. Open the oauth_url in your browser and complete login + TOTP. "
            "2. After login, copy the 'code' value from the browser's redirect URL. "
            "3. POST that code to /admin/shoonya/exchange-code"
        ),
    }


@router.post("/exchange-code")
async def exchange_code(body: CodeExchangeRequest, request: Request, current_user=Depends(get_current_user)):
    """
    Step 2 of OAuth flow.
    Exchanges the browser code for a session token, saves it to .env,
    and activates the Shoonya connection on the running server. Uses the
    same shared connection instance as the background refresh loop, so the
    loop sees this login instead of starting a competing session.
    """
    from marketengine.ShoonyaConnection import activate_market_feeds, get_or_create_connection
    shoonya = get_or_create_connection(request.app)
    loop = asyncio.get_running_loop()

    token = await loop.run_in_executor(None, shoonya.exchange_code, body.code)
    if not token:
        raise HTTPException(status_code=400, detail="Token exchange failed — check server logs for details.")

    connected = await loop.run_in_executor(None, shoonya.connect_with_token, token)
    if not connected:
        raise HTTPException(status_code=400, detail="Token obtained but connection verification failed — token may be invalid.")

    request.app.state.shoonya = shoonya
    activate_market_feeds(request.app)
    logger.info("[admin_shoonya] Shoonya connected via admin OAuth flow")
    return {"status": "connected", "message": "Shoonya connected and token saved to .env."}


POSITION_BOOK_TIMEOUT_SECS = 5.0


@router.get("/positions")
async def get_master_positions(request: Request, current_user=Depends(get_current_user)):
    """
    Shoonya's actual position book for the master account (PositionBook),
    rows passed through exactly as the broker returns them (tsym, netqty,
    netavgprc, rpnl, urmtom, lp, ...) - for reconciling the internal
    per-user positions against what the broker really holds.

    Response: {"success": true, "count": 2, "positions": [...], "as_of": "2026-10-06T10:15:30.123+05:30"}
    """
    shoonya = getattr(request.app.state, "shoonya", None)
    if shoonya is None or not shoonya.is_connected:
        raise HTTPException(status_code=503, detail="Shoonya session is not connected")

    try:
        loop = asyncio.get_running_loop()
        positions = await asyncio.wait_for(
            loop.run_in_executor(None, shoonya.get_position_book),
            timeout=POSITION_BOOK_TIMEOUT_SECS,
        )
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Broker did not respond in time")
    except Exception as exc:
        logger.error(f"[admin_shoonya] position book fetch failed: {exc}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch the broker position book")

    if positions is None:
        raise HTTPException(status_code=502, detail="Broker position book request failed")

    return {
        "success": True,
        "count": len(positions),
        "positions": positions,
        "as_of": datetime.now(IST_OFFSET).isoformat(timespec="milliseconds"),
    }


@router.get("/status")
def get_status(request: Request, current_user=Depends(get_current_user)):
    """Returns current Shoonya connection status."""
    shoonya = getattr(request.app.state, "shoonya", None)
    return {
        "connected": shoonya.is_connected if shoonya else False,
        "has_token": bool(getattr(shoonya, "_session_token", "")) if shoonya else False,
    }
