import asyncio
import logging

from fastapi import APIRouter, Request, HTTPException, Depends
from pydantic import BaseModel
from utils.auth_dependency import get_current_user

router = APIRouter(prefix="/admin/shoonya", tags=["Shoonya Admin"])
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


@router.get("/status")
def get_status(request: Request, current_user=Depends(get_current_user)):
    """Returns current Shoonya connection status."""
    shoonya = getattr(request.app.state, "shoonya", None)
    return {
        "connected": shoonya.is_connected if shoonya else False,
        "has_token": bool(getattr(shoonya, "_session_token", "")) if shoonya else False,
    }
