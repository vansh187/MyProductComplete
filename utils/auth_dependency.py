import logging
import os

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer
from utils.jwt_handler import verify_token

logger = logging.getLogger(__name__)

security = HTTPBearer()

def get_current_user(credentials=Depends(security)):
    token = credentials.credentials

    payload = verify_token(token)

    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token"
        )

    return payload


def _admin_user_ids() -> set[str]:
    """ADMIN_USER_IDS: comma-separated user ids allowed on /admin routes.
    Read per request so it can be changed without a restart."""
    raw = os.getenv("ADMIN_USER_IDS", "")
    return {part.strip() for part in raw.split(",") if part.strip()}


def admin_allowlist_configured() -> bool:
    return bool(_admin_user_ids())


def require_admin(current_user=Depends(get_current_user)):
    """Gate for admin routes (master broker account controls and data).
    There is no role claim in our JWTs, so admins are an optional
    server-side allowlist:
      - ADMIN_USER_IDS empty/unset: any logged-in user is allowed (current
        choice - the team is small and everyone is trusted);
      - ADMIN_USER_IDS set: only those user ids; everyone else gets 403.
    Turning the restriction on later is a .env change plus restart, no code."""
    try:
        allowed = _admin_user_ids()
        if not allowed:
            return current_user
        user_id = str(current_user.get("user_id", "")) if isinstance(current_user, dict) else ""
    except Exception as exc:
        logger.error(f"Admin check failed: {exc}")
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")
    if not user_id or user_id not in allowed:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")
    return current_user
