import os
import asyncio
import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from dotenv import load_dotenv, set_key

import requests as _requests

IST = ZoneInfo("Asia/Kolkata")

_ENV_FILE = Path(__file__).parent.parent / ".env"

try:
    from NorenRestApiPy.NorenApi import NorenApi as _NorenApi
    _NOREN_AVAILABLE = True
except ImportError:
    _NorenApi = object
    _NOREN_AVAILABLE = False
    print("[Shoonya] NorenRestApiOAuth not installed — run: pip install NorenRestApiOAuth")


class _ShoonyaApi(_NorenApi):
    def __init__(self, api_url: str):
        api_url = api_url.rstrip("/")
        ws_url  = api_url.replace("https://", "wss://").replace("NorenWClientAPI", "NorenWSAPI")
        super().__init__(host=api_url, websocket=ws_url)


def _safe_float(val, default: float = 0.0) -> float:
    try:
        return float(val) if val not in (None, "") else default
    except (TypeError, ValueError):
        return default


class ShoonyaConnection:
    """
    Manages a Shoonya (Finvasia) REST API session via OAuth 2.0.

    One-time setup (repeat when token expires — typically daily):
      1. GET  /admin/shoonya/auth-url  → open the returned URL in a browser
      2. Complete login + TOTP on Shoonya's page
      3. Copy the `code` value from the browser's redirect URL
      4. POST /admin/shoonya/exchange-code  { "code": "<paste here>" }
         → saves SHOONYA_SESSION_TOKEN + SHOONYA_ACCESS_TOKEN to .env

    On every subsequent startup the app reads stored tokens and reconnects
    automatically without any browser interaction.

    Required .env keys:
        SHOONYA_USER_ID, SHOONYA_PASSWORD, SHOONYA_VENDOR_CODE,
        SHOONYA_API_SECRET, SHOONYA_IMEI
    Auto-populated after first exchange:
        SHOONYA_SESSION_TOKEN, SHOONYA_ACCESS_TOKEN, SHOONYA_ACCOUNT_ID
    Optional:
        SHOONYA_API_URL  (defaults to Finvasia production endpoint)
    """

    def __init__(self):
        load_dotenv(dotenv_path=_ENV_FILE, override=True)
        self._user_id       = os.getenv("SHOONYA_USER_ID", "")
        self._password      = os.getenv("SHOONYA_PASSWORD", "")
        self._vendor_code   = os.getenv("SHOONYA_VENDOR_CODE", "")   # e.g. FN215083_U
        self._api_key       = os.getenv("SHOONYA_API_SECRET", "")    # Secret_Code
        self._imei          = os.getenv("SHOONYA_IMEI", "")
        self._api_url       = os.getenv("SHOONYA_API_URL", "https://api.shoonya.com/NorenWClientAPI/")
        self._session_token = os.getenv("SHOONYA_SESSION_TOKEN", "")  # susertoken
        self._access_token  = os.getenv("SHOONYA_ACCESS_TOKEN", "")   # Bearer token
        self._account_id    = os.getenv("SHOONYA_ACCOUNT_ID", self._user_id)
        self._api: _ShoonyaApi | None = None
        self._connected = False

    # ------------------------------------------------------------------
    # OAuth helpers
    # ------------------------------------------------------------------

    def get_oauth_url(self) -> str:
        """Browser URL the user opens to authenticate with Shoonya."""
        oauth_base = "https://api.shoonya.com/OAuthlogin/authorize/oauth"
        return f"{oauth_base}?client_id={self._vendor_code}"

    def exchange_code(self, auth_code: str) -> str | None:
        """
        Exchange OAuth authorization code for session + access tokens.
        Checksum formula (from NorenRestApiOAuth source):
            sha256(vendor_code + api_secret + auth_code)

        Saves SHOONYA_SESSION_TOKEN and SHOONYA_ACCESS_TOKEN to .env.
        Returns susertoken on success, None on failure.
        """
        # Correct checksum: sha256(client_id + Secret_Code + authcode)
        data_to_hash = (self._vendor_code + self._api_key + auth_code).encode("utf-8")
        checksum     = hashlib.sha256(data_to_hash).hexdigest()

        values  = {"code": auth_code, "checksum": checksum, "uid": self._user_id}
        payload = "jData=" + json.dumps(values)
        url     = self._api_url.rstrip("/") + "/GenAcsTok"

        try:
            print(f"[Shoonya] Exchanging code at {url}")
            r = _requests.post(url, data=payload, timeout=15)
            print(f"[Shoonya] {r.status_code}: {r.text[:300]}")

            data = r.json()
            if "access_token" not in data:
                print(f"[Shoonya] Token exchange failed: {data}")
                return None

            acc_tok      = data["access_token"]
            susertoken   = data.get("susertoken") or acc_tok   # OAuth v2 may omit susertoken
            account_id   = data.get("actid") or data.get("USERID") or self._user_id

            # Persist to .env
            set_key(str(_ENV_FILE), "SHOONYA_SESSION_TOKEN", susertoken)
            set_key(str(_ENV_FILE), "SHOONYA_ACCESS_TOKEN", acc_tok)
            set_key(str(_ENV_FILE), "SHOONYA_ACCOUNT_ID", account_id)

            self._session_token = susertoken
            self._access_token  = acc_tok
            self._account_id    = account_id

            print("[Shoonya] Tokens saved to .env")
            return susertoken

        except Exception as exc:
            print(f"[Shoonya] exchange_code error: {exc}")
            return None

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def connect(self) -> bool:
        """
        Connect using stored tokens from .env.
        Returns True if the session is valid and quotes work.
        """
        self._connected = False

        if not _NOREN_AVAILABLE:
            print("[Shoonya] NorenRestApiOAuth missing.")
            return False

        if not self._session_token:
            print(
                "[Shoonya] No session token. Complete OAuth flow:\n"
                "  1. GET  /admin/shoonya/auth-url  → open URL in browser\n"
                "  2. POST /admin/shoonya/exchange-code  {\"code\": \"<paste>\"}"
            )
            return False

        try:
            self._api = _ShoonyaApi(self._api_url)

            # Restore session (NorenRestApiOAuth requires accesstoken too)
            self._api.set_session(
                userid=self._user_id,
                password=self._password,
                usertoken=self._session_token,
                accesstoken=self._access_token,
            )

            # Inject OAuth Bearer header
            if self._access_token:
                self._api.injectOAuthHeader(
                    self._access_token,
                    self._user_id,
                    self._account_id,
                )

            # Verify with a live Nifty quote
            test = self._api.get_quotes(exchange="NSE", token="26000")
            if test and test.get("stat") == "Ok":
                self._connected = True
                print(f"[Shoonya] Connected as {self._user_id} (pid={os.getpid()})")
                return True

            print(f"[Shoonya] Token invalid or expired: {test}")
            return False

        except Exception as exc:
            print(f"[Shoonya] connect error: {exc}")
            return False

    def connect_with_token(self, susertoken: str) -> bool:
        """Connect with a freshly exchanged token (called after exchange_code)."""
        self._session_token = susertoken
        return self.connect()

    # ------------------------------------------------------------------
    # Market data
    # ------------------------------------------------------------------

    def get_index_quote(self, exchange: str, token: str) -> dict | None:
        """
        Returns a normalised quote dict or None on failure.
        Keys: ltp, open, high, low, prev_close, change, change_pct, as_of
        """
        if not self._connected or self._api is None:
            return None
        try:
            ret = self._api.get_quotes(exchange=exchange, token=token)
            if not ret or ret.get("stat") != "Ok":
                print(f"[Shoonya] get_quotes failed {exchange}:{token} → {ret}")
                return None

            ltp = _safe_float(ret.get("lp"))
            if ltp == 0:
                return None

            prev_close = _safe_float(ret.get("c"))
            change     = round(ltp - prev_close, 2)
            pc         = ret.get("pc")
            try:
                change_pct = round(float(pc), 2) if pc not in (None, "") else (
                    round(change / prev_close * 100, 2) if prev_close else 0.0
                )
            except (TypeError, ValueError):
                change_pct = 0.0

            return {
                "ltp":        ltp,
                "open":       _safe_float(ret.get("o")),
                "high":       _safe_float(ret.get("h")),
                "low":        _safe_float(ret.get("l")),
                "prev_close": prev_close,
                "change":     change,
                "change_pct": change_pct,
                "as_of":      ret.get("ltt"),
            }

        except Exception as exc:
            print(f"[Shoonya] get_index_quote error {exchange}:{token}: {exc}")
            return None

    def get_time_price_series(self, exchange: str, token: str, interval: str, days: int = 1) -> list[dict] | None:
        """
        Fetch OHLC candle data from Shoonya for a given timeframe.

        Args:
            exchange: 'NSE' or 'BSE'
            token: Security token (e.g. '26000' for Nifty 50)
            interval: Candle interval ('1minute', '3minute', '5minute', '15minute', '1hour', '1day')
            days: Number of days of history to fetch (default 1 = today)

        Returns:
            List of dicts with keys: timestamp, open, high, low, close, volume
            OR None on failure
        """
        if not self._connected or self._api is None:
            return None

        try:
            ret = self._api.get_time_price_series(
                exchange=exchange,
                token=token,
                starttime=0,
                interval=interval,
                lastn=500  # Get last 500 candles (covers ~8 hours of 1m data)
            )

            if not ret or ret.get("stat") != "Ok":
                print(f"[Shoonya] get_time_price_series failed {exchange}:{token} interval={interval} → {ret}")
                return None

            candles = ret.get("jdata", [])
            if not candles:
                return None

            # Parse Shoonya's timestamp format and normalize
            result = []
            for candle in candles:
                try:
                    result.append({
                        "timestamp": candle.get("time"),  # Already ISO-8601 from Shoonya
                        "open":      _safe_float(candle.get("o")),
                        "high":      _safe_float(candle.get("h")),
                        "low":       _safe_float(candle.get("l")),
                        "close":     _safe_float(candle.get("c")),
                        "volume":    int(candle.get("v", 0)) if candle.get("v") else 0,
                    })
                except Exception as e:
                    print(f"[Shoonya] Error parsing candle {candle}: {e}")
                    continue

            return result if result else None

        except Exception as exc:
            print(f"[Shoonya] get_time_price_series error {exchange}:{token} interval={interval}: {exc}")
            return None

    # ------------------------------------------------------------------
    # Automated login (direct HTTP — no browser required)
    # ------------------------------------------------------------------

    def auto_login(self) -> bool:
        """
        Direct HTTP login via Shoonya's classic /QuickAuth endpoint.
        Reads credentials + TOTP secret from .env and logs in with a
        single POST request — no Chrome/Selenium dependency, so it works
        on memory-constrained hosts where headless Chrome can't launch.
        """
        try:
            import pyotp
        except ImportError as e:
            print(f"[Shoonya] auto_login dependency missing: {e}")
            return False

        load_dotenv(dotenv_path=_ENV_FILE, override=True)
        totp_secret = os.getenv("SHOONYA_TOTP_SECRET", "")
        if not totp_secret:
            print("[Shoonya] SHOONYA_TOTP_SECRET not set — cannot auto_login")
            return False

        print(f"[Shoonya] auto_login starting (pid={os.getpid()})")

        pwd_hash     = hashlib.sha256(self._password.encode("utf-8")).hexdigest()
        app_key_hash = hashlib.sha256(f"{self._user_id}|{self._api_key}".encode("utf-8")).hexdigest()
        totp_val     = pyotp.TOTP(totp_secret).now()

        payload = {
            "apkversion": "1.0.0",
            "uid":        self._user_id,
            "pwd":        pwd_hash,
            "factor2":    totp_val,
            "vc":         self._vendor_code,
            "appkey":     app_key_hash,
            "imei":       self._imei,
            "source":     "API",
        }
        url = self._api_url.rstrip("/") + "/QuickAuth"

        try:
            r    = _requests.post(url, data="jData=" + json.dumps(payload), timeout=15)
            resp = r.json()
        except Exception as exc:
            print(f"[Shoonya] auto_login request failed: {exc}")
            return False

        if resp.get("stat") != "Ok":
            print(f"[Shoonya] auto_login rejected: {resp.get('emsg', resp)}")
            return False

        susertoken = resp.get("susertoken", "")
        account_id = resp.get("actid") or self._user_id
        if not susertoken:
            print(f"[Shoonya] auto_login: no susertoken in response: {resp}")
            return False

        # Persist — access_token has no separate value in the classic
        # QuickAuth flow, so susertoken is reused as the Bearer token,
        # matching exchange_code()'s OAuth-v2 fallback behavior.
        set_key(str(_ENV_FILE), "SHOONYA_SESSION_TOKEN", susertoken)
        set_key(str(_ENV_FILE), "SHOONYA_ACCESS_TOKEN", susertoken)
        set_key(str(_ENV_FILE), "SHOONYA_ACCOUNT_ID", account_id)

        self._account_id   = account_id
        self._access_token  = susertoken

        print("[Shoonya] auto_login: token saved to .env, verifying session...")
        return self.connect_with_token(susertoken)

    def invalidate(self) -> None:
        """Marks the session as disconnected (e.g. right before a forced refresh),
        so dependent endpoints correctly 503 instead of silently using a dead token."""
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected


# ------------------------------------------------------------------
# Daily auto-refresh scheduler (called from app.py lifespan)
# ------------------------------------------------------------------

def _next_refresh_delay() -> float:
    """Seconds until next weekday 8:30 AM IST (before market opens at 9:15)."""
    now       = datetime.now(IST)
    candidate = now.replace(hour=8, minute=30, second=0, microsecond=0)
    if now >= candidate:
        candidate += timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return (candidate - now).total_seconds()


async def schedule_daily_refresh(app):
    """
    Background task that keeps the Shoonya session alive.

    - If not currently connected (startup connect/auto_login failed, or a
      previous scheduled refresh failed), retries auto_login() every
      RETRY_DELAY seconds until it succeeds. This prevents a single
      transient failure (Chrome crash, TOTP timing, etc.) from causing an
      all-day outage — previously a failed 8:30 AM refresh wasn't retried
      until the next weekday's 8:30 AM slot.
    - Once connected, sleeps until the next weekday 8:30 AM IST (Shoonya
      invalidates the previous session around market pre-open), marks the
      session disconnected, and forces a fresh auto_login().
    """
    RETRY_DELAY = 300  # 5 minutes

    async def _auto_login(shoonya) -> bool:
        loop = asyncio.get_running_loop()
        try:
            ok = await loop.run_in_executor(None, shoonya.auto_login)
        except Exception as exc:
            # Never let an unexpected auto_login exception kill this
            # background task — that would silently stop all future
            # reconnect attempts until the process is restarted.
            print(f"[Shoonya] auto_login raised unexpectedly: {exc}")
            ok = False
        if ok:
            app.state.shoonya = shoonya
        return ok

    while True:
        shoonya = getattr(app.state, "shoonya", None)
        if shoonya is None:
            from marketengine.ShoonyaConnection import ShoonyaConnection
            shoonya = ShoonyaConnection()

        while not shoonya.is_connected:
            print(f"[Shoonya] Disconnected — attempting auto-login... (pid={os.getpid()})")
            if await _auto_login(shoonya):
                print(f"[Shoonya] Reconnected at {datetime.now(IST).strftime('%H:%M IST')}")
                break
            print(f"[Shoonya] Auto-login failed — retrying in {RETRY_DELAY // 60} min")
            await asyncio.sleep(RETRY_DELAY)

        delay   = _next_refresh_delay()
        next_at = datetime.now(IST) + timedelta(seconds=delay)
        print(
            f"[Shoonya] Next scheduled refresh at "
            f"{next_at.strftime('%Y-%m-%d %H:%M IST')} "
            f"({delay / 3600:.1f}h from now)"
        )
        await asyncio.sleep(delay)

        # Shoonya invalidates sessions around this time — mark disconnected
        # immediately so endpoints correctly 503 (instead of silently using
        # a dead token) until the refresh below completes or the retry loop
        # above picks it back up.
        shoonya.invalidate()
        if await _auto_login(shoonya):
            print(f"[Shoonya] Scheduled auto-refresh succeeded at {datetime.now(IST).strftime('%H:%M IST')}")
        else:
            print("[Shoonya] Scheduled auto-refresh failed — entering retry mode")
