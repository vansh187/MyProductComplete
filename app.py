import asyncio
import logging
import time
from fastapi import FastAPI
from api.signup import router as signup_router
from api.login import router as login_router
from api.orders import router as orders_router
from api.trade import router as trade_history
from api.portfolio import router as user_portfolio
from api.VerifyFundTransaction import router as verify_transaction
from contextlib import asynccontextmanager
from api.Dashboard import router as dashboardRouter
from api.AddfundstoWallet import router as razorPayPaymentRouter
from api.marketquotes import router as marketQuotesRouter
from api.auth_google import router as googleAuthRouter
from api.admin_shoonya import router as adminShoonyaRouter
from api.sectorPerformance import router as sectorPerformanceRouter
from api.topMovers import router as topMoversRouter, start_background_refresh as top_movers_refresh
from api.candles import router as candlesRouter
from api.optionChain import router as optionChainRouter, _optionChainService
from appconfig.OptionMaster import schedule_daily_refresh as option_master_daily_refresh
from fastapi.middleware.cors import CORSMiddleware
try:
    from breeze_connect import BreezeConnect
    from marketengine.config import Config
    from marketengine.BreezeSessionManager import schedule_daily_refresh
    _BREEZE_IMPORTABLE = True
except Exception as _breeze_import_err:
    BreezeConnect = None
    Config = None
    schedule_daily_refresh = None
    _BREEZE_IMPORTABLE = False
    print(f"[WARNING] breeze_connect import failed — market data disabled: {_breeze_import_err}")

try:
    from marketengine.ShoonyaConnection import ShoonyaConnection, schedule_daily_refresh as shoonya_daily_refresh
    from marketengine.ShoonyaOptionFeed import ShoonyaOptionFeed
    _SHOONYA_IMPORTABLE = True
except Exception as _shoonya_import_err:
    ShoonyaConnection = None
    shoonya_daily_refresh = None
    ShoonyaOptionFeed = None
    _SHOONYA_IMPORTABLE = False
    print(f"[WARNING] ShoonyaConnection import failed: {_shoonya_import_err}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    refresh_task         = None
    shoonya_refresh_task = None
    top_movers_task      = None
    option_master_task   = None
    app.state.breeze       = None
    app.state.shoonya      = None
    app.state.option_feed  = None

    # ── Shoonya (primary indices provider) ───────────────────────────
    if _SHOONYA_IMPORTABLE:
        try:
            shoonya = ShoonyaConnection()
            if shoonya.connect():
                app.state.shoonya = shoonya
            else:
                print("[WARNING] Shoonya stored token invalid — attempting auto-login...")
                loop = asyncio.get_running_loop()
                ok   = await loop.run_in_executor(None, shoonya.auto_login)
                if ok:
                    app.state.shoonya = shoonya
                else:
                    print("[WARNING] Shoonya auto-login failed — will keep retrying in the background")
        except Exception as e:
            print(f"[WARNING] Shoonya init error: {e}")

        # Always start the refresh loop, even if the connection attempt
        # above failed or raised — it will retry auto_login on a short
        # interval instead of leaving the app permanently disconnected
        # until a manual restart or the next scheduled 8:30 AM slot.
        shoonya_refresh_task = asyncio.create_task(shoonya_daily_refresh(app))
        top_movers_task      = asyncio.create_task(top_movers_refresh(app))

        # ── Option chain: live WS feed + scrip-master daily refresh ──
        if app.state.shoonya is not None:
            try:
                option_feed = ShoonyaOptionFeed(app.state.shoonya)
                option_feed.start()
                _optionChainService.set_feed(option_feed)
                app.state.option_feed = option_feed
                print("App starting... Option chain WebSocket feed started.")
            except Exception as e:
                print(f"[WARNING] Option chain feed init error: {e}")
        option_master_task = asyncio.create_task(option_master_daily_refresh(app))
    else:
        print("App starting... Shoonya unavailable.")

    # ── Breeze (secondary / stock quotes) ────────────────────────────
    if app.state.shoonya is not None:
        print("App starting... Breeze skipped (Shoonya is primary).")
    elif _BREEZE_IMPORTABLE:
        print("App starting... Establishing Breeze session")
        try:
            breeze = BreezeConnect(api_key=Config.BREEZE_API_KEY)
            breeze.generate_session(
                api_secret=Config.BREEZE_SECRET_KEY,
                session_token=Config.BREEZE_SESSION_TOKEN
            )
            app.state.breeze = breeze
            print("Breeze session ready.")
            refresh_task = asyncio.create_task(schedule_daily_refresh(app))
        except Exception as e:
            print(f"[WARNING] Breeze session failed — stock quotes will be unavailable: {e}")
    else:
        print("App starting... Breeze unavailable.")

    yield

    if refresh_task:
        refresh_task.cancel()
    if shoonya_refresh_task:
        shoonya_refresh_task.cancel()
    if top_movers_task:
        top_movers_task.cancel()
    if option_master_task:
        option_master_task.cancel()
    if app.state.option_feed:
        app.state.option_feed.close()
    print("Server shutting down.")


app = FastAPI(lifespan=lifespan)
app.include_router(orders_router)
app.include_router(signup_router)
app.include_router(login_router)
app.include_router(trade_history)
app.include_router(user_portfolio)
app.include_router(dashboardRouter)
app.include_router(razorPayPaymentRouter)
app.include_router(verify_transaction)
app.include_router(marketQuotesRouter)
app.include_router(googleAuthRouter)
app.include_router(adminShoonyaRouter)
app.include_router(sectorPerformanceRouter)
app.include_router(topMoversRouter)
app.include_router(candlesRouter)
app.include_router(optionChainRouter)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "https://myproductreact.onrender.com", "https://primepiptrade.com", "https://www.primepiptrade.com"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


logger = logging.getLogger("latency")
logger.setLevel(logging.INFO)
if not logger.handlers:
    # Independent of any other logging config in the app (most of which
    # uses plain print()) - guarantees these lines reach stdout, which
    # systemd/journald already captures, without depending on root logger
    # setup or double-printing if this module is ever imported twice.
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(asctime)s [latency] %(message)s"))
    logger.addHandler(_handler)
    logger.propagate = False

# Requests slower than this get an explicit warning line (grep "SLOW" in the
# journal) instead of blending into the routine per-request log noise -
# these are exactly the ones worth investigating first.
SLOW_REQUEST_THRESHOLD_MS = 1000


@app.middleware("http")
async def add_latency_tracking(request, call_next):
    """Times every request end-to-end (including any broker/DB round-trips
    the handler makes) with negligible overhead (a single perf_counter read
    on each side of the call). Exposes it two ways:
      - "Server-Timing" response header: visible directly in the browser's
        Network tab (Timing panel) per request, no extra tooling needed.
      - A log line per request, so latency can be grepped/aggregated from
        the server's own logs (journalctl -u primepiptrade-api) without
        needing a separate APM stack.
    """
    start = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        duration_ms = (time.perf_counter() - start) * 1000
        logger.error(f"{request.method} {request.url.path} FAILED after {duration_ms:.1f}ms")
        raise

    duration_ms = (time.perf_counter() - start) * 1000
    response.headers["Server-Timing"] = f"app;dur={duration_ms:.1f}"
    response.headers["X-Response-Time-Ms"] = f"{duration_ms:.1f}"

    log_line = f"{request.method} {request.url.path} {response.status_code} {duration_ms:.1f}ms"
    if duration_ms >= SLOW_REQUEST_THRESHOLD_MS:
        logger.warning(f"SLOW {log_line}")
    else:
        logger.info(log_line)

    return response


@app.middleware("http")
async def add_coop_header(request, call_next):
    response = await call_next(request)
    response.headers["Cross-Origin-Opener-Policy"] = "same-origin-allow-popups"
    return response


@app.api_route("/", methods=["GET", "HEAD"])
def read_root():
    return {"Message": "Finnaly I am able to run my first API"}
