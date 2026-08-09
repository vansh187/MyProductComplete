import asyncio
import logging
import os
import time
import httpx
from fastapi import FastAPI
from api.signup import router as signup_router
from api.login import router as login_router
from api.orders import router as orders_router
from api.trade import router as trade_history
from api.portfolio import router as user_portfolio
from api.positions import router as positionsRouter
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
from service.positionTickService import positionTickService
from service.orderUpdateService import OrderUpdateService
from appconfig.OptionMaster import schedule_daily_refresh as option_master_daily_refresh
from appconfig.FutureMaster import schedule_daily_refresh as future_master_daily_refresh
from api.mutualFunds import router as mutualFundsRouter
from mutualfunds.backfill_service import MFNavBackfillService
from mutualfunds.cache import MFInMemoryCache
from mutualfunds.collections_config import MFCollectionsCatalog
from mutualfunds.curation.curated_picks_repository import MFCuratedPicksRepository
from mutualfunds.curation.fallback_provider import RankedFallbackCurationProvider
from mutualfunds.curation_service import MFCurationService
from mutualfunds.daily_sync_service import MFDailyNavSyncService
from mutualfunds.nav_history_repository import MFNavHistoryRepository
from mutualfunds.providers.mfapi_provider import MfApiInProvider
from mutualfunds.repository import MFSchemeRepository
from mutualfunds.returns_calculator import MFReturnsCalculator
from mutualfunds.returns_repository import MFReturnsRepository
from mutualfunds.scheduler import (
    MutualFundBackgroundJobRunner,
    schedule_daily_refresh as mutual_fund_daily_refresh,
    schedule_cache_eviction as mutual_fund_cache_eviction_refresh,
)
from mutualfunds.service import MutualFundService
from mutualfunds.sync_service import MFSchemeMasterSyncService
from fastapi.middleware.cors import CORSMiddleware

try:
    from mutualfunds.curation.gemini_provider import GeminiCurationProvider
    _GEMINI_IMPORTABLE = True
except Exception as _gemini_import_err:
    GeminiCurationProvider = None
    _GEMINI_IMPORTABLE = False
    print(f"[WARNING] Gemini curation provider unavailable: {_gemini_import_err}")

try:
    from mutualfunds.curation.groq_provider import GroqCurationProvider
    _GROQ_IMPORTABLE = True
except Exception as _groq_import_err:
    GroqCurationProvider = None
    _GROQ_IMPORTABLE = False
    print(f"[WARNING] Groq curation provider unavailable: {_groq_import_err}")
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

lifespan_logger = logging.getLogger("lifespan")

# Broker session-establishment calls (shoonya.connect/auto_login,
# breeze.generate_session) are synchronous SDK calls with no built-in
# timeout - an unresponsive broker endpoint during startup would otherwise
# block the app from ever finishing lifespan and accepting requests
# (health checks would fail forever instead of the app degrading gracefully
# to "broker unavailable, will keep retrying"). Bounded here the same way
# every other broker REST call in this codebase already is (see
# service/optionChain/OptionChainService.py, api/marketquotes.py).
SHOONYA_CONNECT_TIMEOUT_SECS = 15.0
SHOONYA_AUTO_LOGIN_TIMEOUT_SECS = 90.0  # Chrome-automation OAuth flow - genuinely slower than a plain API call
BREEZE_SESSION_TIMEOUT_SECS = 15.0

# How long to wait before restarting a crashed background refresh loop -
# short enough to recover quickly, long enough not to hot-loop against a
# persistently failing dependency (e.g. broker endpoint down for an hour).
BACKGROUND_TASK_RESTART_DELAY_SECS = 30.0


async def _supervised_background_task(coro_func, name: str, app: FastAPI) -> None:
    """
    Wraps a lifespan background coroutine (a daily-refresh loop) so an
    exception escaping it logs and restarts the loop after a backoff,
    instead of silently terminating the task forever. A naked
    asyncio.create_task(coro) means any unhandled exception inside the
    coroutine kills that task permanently with nothing else surfacing it -
    the app keeps serving requests and looks perfectly healthy while a
    critical background job (broker session refresh, scrip master refresh)
    has quietly stopped running until someone notices and restarts the
    whole process. This makes that class of bug self-healing instead.
    """
    while True:
        try:
            await coro_func(app)
            # A well-behaved refresh loop is itself an infinite `while True`
            # and should never return normally - if it does, restarting
            # would just do the same thing again, so stop here rather than
            # busy-loop forever on a no-op coroutine.
            lifespan_logger.warning(f"[{name}] background task returned without raising - not restarting")
            return
        except asyncio.CancelledError:
            raise
        except Exception as ex:
            lifespan_logger.error(
                f"[{name}] background task crashed: {ex} - restarting in "
                f"{BACKGROUND_TASK_RESTART_DELAY_SECS}s"
            )
            await asyncio.sleep(BACKGROUND_TASK_RESTART_DELAY_SECS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    refresh_task         = None
    shoonya_refresh_task = None
    top_movers_task      = None
    option_master_task   = None
    future_master_task   = None
    mutual_fund_task      = None
    mutual_fund_cache_eviction_task = None
    app.state.breeze       = None
    app.state.shoonya      = None
    app.state.option_feed  = None
    app.state.mutual_fund_service     = None
    app.state.mutual_fund_job_runner  = None
    app.state.mf_http_client          = None
    app.state.mf_cache                = None

    # ── Mutual Funds (mfapi.in-backed, Supabase-owned, LLM-curated) ──
    try:
        mf_http_client = httpx.AsyncClient(timeout=30.0)
        mf_provider = MfApiInProvider(mf_http_client)
        mf_scheme_repo = MFSchemeRepository()
        mf_nav_repo = MFNavHistoryRepository()
        mf_returns_repo = MFReturnsRepository()
        mf_picks_repo = MFCuratedPicksRepository()
        mf_cache = MFInMemoryCache()
        mf_calculator = MFReturnsCalculator()
        mf_collections = MFCollectionsCatalog()

        app.state.mf_http_client = mf_http_client
        app.state.mf_cache = mf_cache
        app.state.mutual_fund_service = MutualFundService(
            scheme_repository=mf_scheme_repo,
            nav_history_repository=mf_nav_repo,
            returns_repository=mf_returns_repo,
            curated_picks_repository=mf_picks_repo,
            cache=mf_cache,
            provider=mf_provider,
            returns_calculator=mf_calculator,
            collections_catalog=mf_collections,
        )

        # Curation providers: Gemini primary, Groq secondary, plain-ranked
        # fallback last - degrades to the fallback whenever a key is
        # missing or the corresponding SDK failed to import, so the
        # Explore page is never blocked on LLM availability.
        fallback_provider = RankedFallbackCurationProvider()
        gemini_key = os.getenv("GEMINI_API_KEY")
        groq_key = os.getenv("GROQ_API_KEY")
        primary_provider = (
            GeminiCurationProvider(gemini_key) if (_GEMINI_IMPORTABLE and gemini_key) else fallback_provider
        )
        secondary_provider = (
            GroqCurationProvider(groq_key) if (_GROQ_IMPORTABLE and groq_key) else fallback_provider
        )

        curation_service = MFCurationService(
            returns_repository=mf_returns_repo,
            curated_picks_repository=mf_picks_repo,
            collections_catalog=mf_collections,
            primary_provider=primary_provider,
            secondary_provider=secondary_provider,
            fallback_provider=fallback_provider,
        )
        app.state.mutual_fund_job_runner = MutualFundBackgroundJobRunner(
            master_sync=MFSchemeMasterSyncService(mf_provider, mf_scheme_repo),
            backfill=MFNavBackfillService(mf_provider, mf_scheme_repo, mf_nav_repo, mf_returns_repo, mf_calculator),
            daily_sync=MFDailyNavSyncService(mf_provider, mf_scheme_repo, mf_nav_repo, mf_returns_repo, mf_calculator),
            curation=curation_service,
        )
        mutual_fund_task = asyncio.create_task(
            _supervised_background_task(mutual_fund_daily_refresh, "mutual_fund_refresh", app)
        )
        mutual_fund_cache_eviction_task = asyncio.create_task(
            _supervised_background_task(mutual_fund_cache_eviction_refresh, "mutual_fund_cache_eviction", app)
        )
        print("App starting... Mutual funds module initialized.")
    except Exception as e:
        print(f"[WARNING] Mutual funds module init error: {e}")

    # ── Shoonya (primary indices provider) ───────────────────────────
    if _SHOONYA_IMPORTABLE:
        try:
            shoonya = ShoonyaConnection()
            loop = asyncio.get_running_loop()
            connected = await asyncio.wait_for(
                loop.run_in_executor(None, shoonya.connect),
                timeout=SHOONYA_CONNECT_TIMEOUT_SECS
            )
            if connected:
                app.state.shoonya = shoonya
            else:
                print("[WARNING] Shoonya stored token invalid — attempting auto-login...")
                ok = await asyncio.wait_for(
                    loop.run_in_executor(None, shoonya.auto_login),
                    timeout=SHOONYA_AUTO_LOGIN_TIMEOUT_SECS
                )
                if ok:
                    app.state.shoonya = shoonya
                else:
                    print("[WARNING] Shoonya auto-login failed — will keep retrying in the background")
        except asyncio.TimeoutError:
            print("[WARNING] Shoonya connect/auto-login timed out during startup — will keep retrying in the background")
        except Exception as e:
            print(f"[WARNING] Shoonya init error: {e}")

        # Always start the refresh loop, even if the connection attempt
        # above failed or raised — it will retry auto_login on a short
        # interval instead of leaving the app permanently disconnected
        # until a manual restart or the next scheduled 8:30 AM slot.
        shoonya_refresh_task = asyncio.create_task(
            _supervised_background_task(shoonya_daily_refresh, "shoonya_refresh", app)
        )
        top_movers_task = asyncio.create_task(
            _supervised_background_task(top_movers_refresh, "top_movers_refresh", app)
        )

        # ── Option chain: live WS feed + scrip-master daily refresh ──
        if app.state.shoonya is not None:
            try:
                option_feed = ShoonyaOptionFeed(app.state.shoonya)
                option_feed.start()
                _optionChainService.set_feed(option_feed)
                positionTickService.set_feed(option_feed)
                # Live Shoonya order placement (POST /createLiveOrder) - real
                # fill/reject/cancel notifications for the master account
                # arrive on this SAME WebSocket (Shoonya's order updates and
                # price ticks share one connection) and are the sole source
                # of truth for those orders' positions/status.
                order_update_service = OrderUpdateService()
                option_feed.on_order_update(order_update_service.handle_order_update)
                app.state.order_update_service = order_update_service
                app.state.option_feed = option_feed
                print("App starting... Option chain WebSocket feed started.")
            except Exception as e:
                print(f"[WARNING] Option chain feed init error: {e}")
        option_master_task = asyncio.create_task(
            _supervised_background_task(option_master_daily_refresh, "option_master_refresh", app)
        )
        future_master_task = asyncio.create_task(
            _supervised_background_task(future_master_daily_refresh, "future_master_refresh", app)
        )
    else:
        print("App starting... Shoonya unavailable.")

    # ── Breeze (secondary / stock quotes) ────────────────────────────
    if app.state.shoonya is not None:
        print("App starting... Breeze skipped (Shoonya is primary).")
    elif _BREEZE_IMPORTABLE:
        print("App starting... Establishing Breeze session")
        try:
            breeze = BreezeConnect(api_key=Config.BREEZE_API_KEY)
            loop = asyncio.get_running_loop()
            await asyncio.wait_for(
                loop.run_in_executor(
                    None,
                    lambda: breeze.generate_session(
                        api_secret=Config.BREEZE_SECRET_KEY,
                        session_token=Config.BREEZE_SESSION_TOKEN
                    )
                ),
                timeout=BREEZE_SESSION_TIMEOUT_SECS
            )
            app.state.breeze = breeze
            print("Breeze session ready.")
            refresh_task = asyncio.create_task(
                _supervised_background_task(schedule_daily_refresh, "breeze_refresh", app)
            )
        except asyncio.TimeoutError:
            print("[WARNING] Breeze session establishment timed out — stock quotes will be unavailable")
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
    if future_master_task:
        future_master_task.cancel()
    if mutual_fund_task:
        mutual_fund_task.cancel()
    if mutual_fund_cache_eviction_task:
        mutual_fund_cache_eviction_task.cancel()
    if app.state.option_feed:
        app.state.option_feed.close()
    if app.state.mf_http_client is not None:
        await app.state.mf_http_client.aclose()
    print("Server shutting down.")


app = FastAPI(lifespan=lifespan)
app.include_router(orders_router)
app.include_router(signup_router)
app.include_router(login_router)
app.include_router(trade_history)
app.include_router(user_portfolio)
app.include_router(positionsRouter)
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
app.include_router(mutualFundsRouter)
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
