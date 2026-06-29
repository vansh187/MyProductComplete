import os
import asyncio
import hashlib
import json
import time
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
                print(f"[Shoonya] Connected as {self._user_id}")
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

    # ------------------------------------------------------------------
    # Automated headless login (no browser interaction needed)
    # ------------------------------------------------------------------

    def auto_login(self) -> bool:
        """
        Fully automated OAuth login using headless Chrome + TOTP.
        Reads credentials from .env, captures the auth code from
        Shoonya's network response, exchanges it for tokens, and
        connects — all without any human interaction.

        Requires: selenium, webdriver-manager, pyotp (all in requirements.txt)
        """
        try:
            import pyotp
            from selenium import webdriver
            from selenium.webdriver.common.by import By
            from selenium.webdriver.common.keys import Keys
            from selenium.webdriver.support.ui import WebDriverWait
            from selenium.webdriver.support import expected_conditions as EC
            from selenium.common.exceptions import WebDriverException, StaleElementReferenceException
            from urllib.parse import urlparse, parse_qs
            from webdriver_manager.chrome import ChromeDriverManager
            from webdriver_manager.core.driver_cache import DriverCacheManager
            from selenium.webdriver.chrome.service import Service
        except ImportError as e:
            print(f"[Shoonya] auto_login dependency missing: {e}")
            return False

        load_dotenv(dotenv_path=_ENV_FILE, override=True)
        totp_secret = os.getenv("SHOONYA_TOTP_SECRET", "")
        if not totp_secret:
            print("[Shoonya] SHOONYA_TOTP_SECRET not set — cannot auto_login")
            return False

        login_url = (
            f"https://api.shoonya.com/OAuthlogin/investor-entry-level/login"
            f"?api_key={self._vendor_code}&route_to={self._user_id}"
        )

        options = webdriver.ChromeOptions()
        options.add_argument("--headless=new")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--window-size=1920,1080")
        options.add_argument("--log-level=3")
        # --no-sandbox is required on Linux servers (GCP/Azure/Render); safe to add on all platforms
        if os.name != "nt":
            options.add_argument("--no-sandbox")
        options.set_capability("goog:loggingPrefs", {"performance": "ALL"})

        # Cache path: D: drive on Windows (C: full), /tmp on Linux
        default_cache = "D:\\webdriver_cache" if os.name == "nt" else "/tmp/webdriver_cache"
        wdm_root = os.environ.get("WDM_CACHE_PATH", default_cache)
        os.makedirs(wdm_root, exist_ok=True)
        _cache = DriverCacheManager(root_dir=wdm_root)

        print("[Shoonya] Starting headless Chrome for auto-login...")
        driver = webdriver.Chrome(
            service=Service(ChromeDriverManager(cache_manager=_cache).install()),
            options=options,
        )
        wait = WebDriverWait(driver, 30)
        auth_code = None

        try:
            driver.get(login_url)
            wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "input")))
            time.sleep(1)

            all_inputs     = driver.find_elements(By.CSS_SELECTOR, "input:not([type='hidden'])")
            visible_inputs = [i for i in all_inputs if i.is_displayed()]

            def _fill(el, val):
                el.click(); time.sleep(0.1); el.clear(); el.send_keys(val); time.sleep(0.1)

            if len(visible_inputs) < 3:
                raise RuntimeError(
                    f"[Shoonya] Expected ≥3 visible inputs on login page, found {len(visible_inputs)}. "
                    f"URL: {driver.current_url}"
                )

            totp_val = pyotp.TOTP(totp_secret).now()
            _fill(visible_inputs[0], self._user_id)
            _fill(visible_inputs[1], self._password)
            _fill(visible_inputs[2], totp_val)

            # Submit via Enter key (avoids fragile button DOM iteration)
            visible_inputs[2].send_keys(Keys.RETURN)

            print("[Shoonya] Credentials submitted, waiting for auth code...")
            deadline = time.time() + 60

            while time.time() < deadline:
                # Primary: check browser's current URL after redirect
                try:
                    current_url = driver.current_url
                    if "code=" in current_url:
                        code = parse_qs(urlparse(current_url).query).get("code", [None])[0]
                        if code:
                            auth_code = code
                            break
                except Exception:
                    pass

                # Fallback: scan Chrome performance logs for redirect URL
                if not auth_code:
                    for entry in driver.get_log("performance"):
                        try:
                            msg = json.loads(entry["message"])["message"]
                            if msg.get("method") == "Network.requestWillBeSent":
                                url = msg.get("params", {}).get("request", {}).get("url", "")
                                if "code=" in url:
                                    code = parse_qs(urlparse(url).query).get("code", [None])[0]
                                    if code:
                                        auth_code = code
                                        break
                        except Exception:
                            continue
                if auth_code:
                    break

                # Re-submit if TOTP rotated before page responded
                new_totp = pyotp.TOTP(totp_secret).now()
                if new_totp != totp_val:
                    try:
                        all_inputs     = driver.find_elements(By.CSS_SELECTOR, "input:not([type='hidden'])")
                        visible_now    = [i for i in all_inputs if i.is_displayed()]
                        if len(visible_now) >= 3:
                            _fill(visible_now[2], new_totp)
                            visible_now[2].send_keys(Keys.RETURN)
                        totp_val = new_totp
                    except (StaleElementReferenceException, Exception):
                        pass

                time.sleep(0.5)

        except Exception as exc:
            print(f"[Shoonya] auto_login browser error: {exc}")
        finally:
            try:
                driver.quit()
            except Exception:
                pass

        if not auth_code:
            print("[Shoonya] auto_login: could not capture auth code")
            return False

        print(f"[Shoonya] Auth code captured, exchanging for token...")
        token = self.exchange_code(auth_code)
        if not token:
            return False

        return self.connect_with_token(token)

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
    Background task: every weekday at 8:30 AM IST, runs auto_login()
    to silently refresh the Shoonya session using headless Chrome + TOTP.
    Falls back to stored tokens if auto_login fails.
    """
    while True:
        delay  = _next_refresh_delay()
        next_at = datetime.now(IST) + timedelta(seconds=delay)
        print(
            f"[Shoonya] Next auto-refresh at "
            f"{next_at.strftime('%Y-%m-%d %H:%M IST')} "
            f"({delay / 3600:.1f}h from now)"
        )
        await asyncio.sleep(delay)

        shoonya = getattr(app.state, "shoonya", None)
        if shoonya is None:
            from marketengine.ShoonyaConnection import ShoonyaConnection
            shoonya = ShoonyaConnection()

        loop = asyncio.get_running_loop()
        ok   = await loop.run_in_executor(None, shoonya.auto_login)
        if ok:
            app.state.shoonya = shoonya
            print(f"[Shoonya] Auto-refresh succeeded at {datetime.now(IST).strftime('%H:%M IST')}")
        else:
            print("[Shoonya] Auto-refresh failed — keeping existing session")
