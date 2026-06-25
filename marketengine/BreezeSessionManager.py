import asyncio
import os
from datetime import datetime, timedelta, time as dt_time
from zoneinfo import ZoneInfo
from breeze_connect import BreezeConnect
from dotenv import load_dotenv, set_key
from pathlib import Path

IST = ZoneInfo("Asia/Kolkata")
_ENV_FILE = Path(__file__).parent.parent / ".env"

# Refresh target: 8:45 AM IST — before market opens at 9:15
_REFRESH_HOUR = 8
_REFRESH_MINUTE = 45


def _next_refresh_delay() -> float:
    """Seconds until the next weekday 8:45 AM IST."""
    now = datetime.now(IST)
    candidate = now.replace(hour=_REFRESH_HOUR, minute=_REFRESH_MINUTE, second=0, microsecond=0)

    if now >= candidate:
        candidate += timedelta(days=1)

    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)

    return (candidate - now).total_seconds()


def refresh_session(app_state, *, session_token: str | None = None) -> bool:
    """
    Re-creates the Breeze session and updates app.state.breeze in-place.

    If session_token is None, re-reads BREEZE_SESSION_TOKEN from .env so the
    caller only needs to update the file — no server restart required.
    Returns True on success, False on failure (server keeps the old session).
    """
    try:
        if session_token is None:
            load_dotenv(dotenv_path=_ENV_FILE, override=True)
            session_token = os.getenv("BREEZE_SESSION_TOKEN")

        if not session_token:
            print("[SessionManager] BREEZE_SESSION_TOKEN is empty — cannot refresh.")
            return False

        api_key    = os.getenv("BREEZE_API_KEY")
        api_secret = os.getenv("BREEZE_SECRET_KEY")

        if not api_key or not api_secret:
            print("[SessionManager] BREEZE_API_KEY / BREEZE_SECRET_KEY missing from env.")
            return False

        breeze = BreezeConnect(api_key=api_key)
        breeze.generate_session(api_secret=api_secret, session_token=session_token)
        app_state.breeze = breeze

        print(
            f"[SessionManager] Breeze session refreshed at "
            f"{datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S IST')}"
        )
        return True

    except Exception as e:
        print(f"[SessionManager] Session refresh failed: {e}")
        return False


def update_env_token(session_token: str) -> None:
    """Writes a new BREEZE_SESSION_TOKEN into the .env file."""
    set_key(str(_ENV_FILE), "BREEZE_SESSION_TOKEN", session_token)
    print(f"[SessionManager] .env updated with new session token.")


async def schedule_daily_refresh(app):
    """
    Background task: waits until 8:45 AM IST on the next weekday, then
    re-reads BREEZE_SESSION_TOKEN from .env and refreshes the Breeze session.
    Repeats indefinitely so it covers every trading day.

    To apply a new token without restarting the server:
      1. Update BREEZE_SESSION_TOKEN in .env  (or call POST /admin/breeze/refresh)
      2. The next scheduled refresh picks it up automatically.
    """
    while True:
        delay = _next_refresh_delay()
        next_at = datetime.now(IST) + timedelta(seconds=delay)
        print(
            f"[SessionManager] Next auto-refresh at "
            f"{next_at.strftime('%Y-%m-%d %H:%M IST')} "
            f"({delay / 3600:.1f}h from now)"
        )
        await asyncio.sleep(delay)
        refresh_session(app.state)
