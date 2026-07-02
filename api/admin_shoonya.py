from fastapi import APIRouter, Request, HTTPException, Depends
from pydantic import BaseModel
from utils.auth_dependency import get_current_user

router = APIRouter(prefix="/admin/shoonya", tags=["Shoonya Admin"])


class CodeExchangeRequest(BaseModel):
    code: str


@router.get("/auth-url")
def get_auth_url(request: Request, current_user=Depends(get_current_user)):
    """
    Step 1 of OAuth flow.
    Returns the browser URL to open for Shoonya authentication.
    """
    from marketengine.ShoonyaConnection import ShoonyaConnection
    shoonya = getattr(request.app.state, "shoonya", None) or ShoonyaConnection()
    return {
        "oauth_url": shoonya.get_oauth_url(),
        "instructions": (
            "1. Open the oauth_url in your browser and complete login + TOTP. "
            "2. After login, copy the 'code' value from the browser's redirect URL. "
            "3. POST that code to /admin/shoonya/exchange-code"
        ),
    }


@router.post("/exchange-code")
def exchange_code(body: CodeExchangeRequest, request: Request, current_user=Depends(get_current_user)):
    """
    Step 2 of OAuth flow.
    Exchanges the browser code for a session token, saves it to .env,
    and activates the Shoonya connection on the running server.
    """
    from marketengine.ShoonyaConnection import ShoonyaConnection
    shoonya = getattr(request.app.state, "shoonya", None) or ShoonyaConnection()

    token = shoonya.exchange_code(body.code)
    if not token:
        raise HTTPException(status_code=400, detail="Token exchange failed — check server logs for details.")

    connected = shoonya.connect_with_token(token)
    if not connected:
        raise HTTPException(status_code=400, detail="Token obtained but connection verification failed — token may be invalid.")

    request.app.state.shoonya = shoonya
    return {"status": "connected", "message": "Shoonya connected and token saved to .env."}


@router.get("/status")
def get_status(request: Request, current_user=Depends(get_current_user)):
    """Returns current Shoonya connection status."""
    shoonya = getattr(request.app.state, "shoonya", None)
    return {
        "connected": shoonya.is_connected if shoonya else False,
        "has_token": bool(shoonya._session_token) if shoonya else False,
    }
