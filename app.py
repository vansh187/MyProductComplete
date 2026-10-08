import asyncio
import ipaddress
import logging
import os
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from dotenv import load_dotenv

from utils.logging_config import setup_logging

# Before any other app import, so import-time log lines are formatted too;
# .env first so LOG_LEVEL / LOG_LEVELS can be set there.
load_dotenv(Path(__file__).parent / ".env")
setup_logging()
app_logger = logging.getLogger("app")

import httpx
from fastapi import FastAPI, Request
from fastapi.middleware.gzip import GZipMiddleware
from api.signup import router as signup_router
from api.login import router as login_router
from api.orders import router as orders_router
from api.trade import router as trade_history
from api.portfolio import router as user_portfolio
from api.positions import router as fnoPositionsRouter
from api.positions import router as positionsRouter
from api.VerifyFundTransaction import router as verify_transaction
from contextlib import asynccontextmanager
from api.Dashboard import router as dashboardRouter, start_equity_curve_capture as equity_curve_refresh
from api.AddfundstoWallet import router as razorPayPaymentRouter
from api.marketquotes import router as marketQuotesRouter, index_instrument_keys
from api.auth_google import router as googleAuthRouter
from api.admin_shoonya import router as adminShoonyaRouter
from api.sectorPerformance import router as sectorPerformanceRouter, sector_instrument_keys
from api.internal import router as internalRouter
from api.topMovers import router as topMoversRouter, start_background_refresh as top_movers_refresh
from api.candles import router as candlesRouter
from api.optionChain import router as optionChainRouter, _optionChainService
from api.margin import router as marginRouter
from utils.auth_dependency import admin_allowlist_configured
from service.positionTickService import positionTickService
from service.orderUpdateService import OrderUpdateService
from appconfig.OptionMaster import schedule_daily_refresh as option_master_daily_refresh
from appconfig.FutureMaster import schedule_daily_refresh as future_master_daily_refresh
from api.mutualFunds import router as mutualFundsRouter
from api.stocks import router as stocksRouter
from api.search import router as searchRouter
from appconfig.StockSymbolMaster import StockSymbolMaster, schedule_daily_refresh as stock_symbol_master_daily_refresh
from marketengine.ShoonyaStockFeed import ShoonyaStockFeed
from service.stocksService.StockCollectionsCatalog import StockCollectionsCatalog
from service.stocksService.StocksService import StocksService
from service.topMovers.TopMoversService import StockWatchlist
from mutualfunds.backfill_service import MFNavBackfillService
from mutualfunds.cache import MFInMemoryCache
from mutualfunds.collections_config import MFCollectionsCatalog
from mutualfunds.curation.curated_picks_repository import MFCuratedPicksRepository
from mutualfunds.curation.fallback_provider import RankedFallbackCurationProvider
from mutualfunds.curation_service import MFCurationService
from mutualfunds.daily_sync_service import MFDailyNavSyncService
from mutualfunds.nav_history_storage import MFNavHistoryParquetStorage
from supabase import create_client as create_supabase_client
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
    app_logger.warning(f"Gemini curation provider unavailable: {_gemini_import_err}")

try:
    from mutualfunds.curation.groq_provider import GroqCurationProvider
    _GROQ_IMPORTABLE = True
except Exception as _groq_import_err:
    GroqCurationProvider = None
    _GROQ_IMPORTABLE = False
    app_logger.warning(f"Groq curation provider unavailable: {_groq_import_err}")
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
    app_logger.warning(f"breeze_connect import failed — market data disabled: {_breeze_import_err}")

try:
    from marketengine.ShoonyaConnection import ShoonyaConnection, schedule_daily_refresh as shoonya_daily_refresh
    from marketengine.ShoonyaOptionFeed import ShoonyaOptionFeed
    _SHOONYA_IMPORTABLE = True
except Exception as _shoonya_import_err:
    ShoonyaConnection = None
    shoonya_daily_refresh = None
    ShoonyaOptionFeed = None
    _SHOONYA_IMPORTABLE = False
    app_logger.warning(f"ShoonyaConnection import failed: {_shoonya_import_err}")

lifespan_logger = logging.getLogger("lifespan")

# Broker session-establishment calls (shoonya.connect,
# breeze.generate_session) are synchronous SDK calls with no built-in
# timeout - an unresponsive broker endpoint during startup would otherwise
# block the app from ever finishing lifespan and accepting requests
# (health checks would fail forever instead of the app degrading gracefully
# to "broker unavailable, will keep retrying"). Bounded here the same way
# every other broker REST call in this codebase already is (see
# service/optionChain/OptionChainService.py, api/marketquotes.py).
SHOONYA_CONNECT_TIMEOUT_SECS = 15.0
BREEZE_SESSION_TIMEOUT_SECS = 15.0

# How long to wait before restarting a crashed background refresh loop -
# short enough to recover quickly, long enough not to hot-loop against a
# persistently failing dependency (e.g. broker endpoint down for an hour).
BACKGROUND_TASK_RESTART_DELAY_SECS = 30.0

# Blocking broker/DB SDK calls are I/O-bound (threads mostly wait on the
# network), so this is sized for concurrency, not CPU count.
BROKER_IO_MAX_WORKERS = 32


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


def _create_market_feeds(app: FastAPI) -> bool:
    """Creates the shared Shoonya WebSocket feed and the stock tick cache
    riding on it, once. Idempotent: returns True without doing anything if
    the feed already exists (restarting an existing feed after a re-login is
    ShoonyaConnection.activate_market_feeds's job). Must run on the event
    loop thread (start() captures the running loop)."""
    if getattr(app.state, "option_feed", None) is not None:
        return True
    shoonya = getattr(app.state, "shoonya", None)
    if shoonya is None or ShoonyaOptionFeed is None:
        return False
    try:
        option_feed = ShoonyaOptionFeed(shoonya)
        option_feed.start()
        _optionChainService.set_feed(option_feed)
        positionTickService.set_feed(option_feed)
        # Live order fills/rejects for the master account arrive on this
        # SAME WebSocket and are the sole source of truth for those orders.
        order_update_service = OrderUpdateService()
        option_feed.on_order_update(order_update_service.handle_order_update)
        app.state.order_update_service = order_update_service
        app.state.option_feed = option_feed
        app_logger.info("Option chain WebSocket feed started.")
    except Exception as e:
        app_logger.error(f"Option chain feed init error: {e}")
        return False

    # Stocks tick cache rides on this SAME WebSocket via on_raw_tick() - see
    # marketengine/ShoonyaStockFeed.py for why it must not open a second socket.
    try:
        stock_feed = ShoonyaStockFeed(option_feed)
        app.state.stock_feed = stock_feed
        # Index spot for the option chain comes from these pinned ticks.
        _optionChainService.set_spot_source(stock_feed)
        app.state.stock_feed_eviction_task = asyncio.create_task(
            _supervised_background_task(
                lambda _app, feed=stock_feed: feed.evict_idle_loop(),
                "stock_feed_idle_eviction", app,
            )
        )
        # Index tiles, sector tiles and the explore watchlist are read on
        # every request: pinned so they are always served from ticks.
        pinned = list(index_instrument_keys()) + sector_instrument_keys()
        stocks_service = getattr(app.state, "stocks_service", None)
        if stocks_service is not None:
            pinned += stocks_service.watchlist_instrument_keys()
        stock_feed.pin(pinned)
        app_logger.info(f"Stock tick cache attached, {len(pinned)} tokens pinned.")
    except Exception as e:
        app_logger.error(f"Stock tick cache init error: {e}")
    return True


def _shutdown_broker_executor(executor: ThreadPoolExecutor) -> None:
    try:
        executor.shutdown(wait=False, cancel_futures=True)
    except Exception as exc:
        app_logger.warning(f"broker executor shutdown error: {exc}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Every run_in_executor(None, ...) broker/DB call in the app lands here.
    # Python's implicit default pool is min(32, cpu+4) threads - 5-6 on a
    # small VM - so one burst of blocking broker calls used to queue every
    # other endpoint's calls behind it.
    broker_executor = ThreadPoolExecutor(max_workers=BROKER_IO_MAX_WORKERS, thread_name_prefix="broker-io")
    asyncio.get_running_loop().set_default_executor(broker_executor)
    app.state.stock_feed_eviction_task = None
    refresh_task         = None
    shoonya_refresh_task = None
    top_movers_task      = None
    option_master_task   = None
    future_master_task   = None
    mutual_fund_task      = None
    mutual_fund_cache_eviction_task = None
    stock_symbol_master_task = None
    app.state.breeze       = None
    app.state.shoonya      = None
    app.state.shoonya_connection = None
    app.state.option_feed  = None
    app.state.stock_feed   = None
    app.state.stock_symbol_master = None
    app.state.stocks_service      = None
    app.state.mutual_fund_service     = None
    app.state.mutual_fund_job_runner  = None
    app.state.mf_http_client          = None
    app.state.mf_cache                = None

    if not admin_allowlist_configured():
        # A deliberate choice for now (small trusted team), but it must stay
        # visible: these routes control the master broker account.
        app_logger.warning(
            "ADMIN_USER_IDS is empty - /admin/shoonya/* (broker OAuth login, master position book) "
            "is open to every logged-in user. Set ADMIN_USER_IDS to restrict it."
        )

    # ── Stocks (Shoonya-backed Explore/Search/Quote/Chart) ────────────
    # Independent of Shoonya being reachable - the symbol master and search
    # work off the bundled/downloaded scrip list alone; only /explore,
    # /quote and /chart need a live broker session, and each of those checks
    # for one itself (StocksService methods accept shoonya=None gracefully).
    try:
        stock_symbol_master = StockSymbolMaster()
        stock_watchlist = StockWatchlist(Path(__file__).parent / "appconfig" / "nifty50_watchlist.json")
        stock_collections = StockCollectionsCatalog(stock_watchlist.stocks())

        app.state.stock_symbol_master = stock_symbol_master
        app.state.stocks_service = StocksService(
            symbol_master=stock_symbol_master,
            explore_watchlist=stock_watchlist,
            collections_catalog=stock_collections,
        )
        stock_symbol_master_task = asyncio.create_task(
            _supervised_background_task(stock_symbol_master_daily_refresh, "stock_symbol_master_refresh", app)
        )
        app_logger.info("Stocks module initialized.")
    except Exception as e:
        app_logger.error(f"Stocks module init error: {e}")

    # ── Mutual Funds (mfapi.in-backed, Supabase-owned, LLM-curated) ──
    try:
        mf_http_client = httpx.AsyncClient(timeout=30.0)
        mf_provider = MfApiInProvider(mf_http_client)
        mf_scheme_repo = MFSchemeRepository()
        # NAV history lives in Supabase Storage as per-scheme Parquet files,
        # not Postgres - mf_nav_history alone was ~750MB (98% of the whole
        # database) and hit the plan's storage cap; Storage carries a
        # separate, larger/cheaper quota and the ~8.7x Parquet compression
        # measured against the same data shrinks the footprint further.
        # Same interface as the old MFNavHistoryRepository (bulk_insert/
        # append_daily/get_series), so nothing downstream changed.
        mf_supabase_client = create_supabase_client(os.getenv("SUPABASE_URL"), os.getenv("SERVICE_ROLE_KEY"))
        mf_nav_repo = MFNavHistoryParquetStorage(mf_supabase_client)
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
        app_logger.info("Mutual funds module initialized.")
    except Exception as e:
        app_logger.error(f"Mutual funds module init error: {e}")

    # ── Shoonya (primary indices provider) ───────────────────────────
    if _SHOONYA_IMPORTABLE:
        try:
            # The one shared instance - the refresh loop and the admin OAuth
            # endpoint resolve this same object (ShoonyaConnection.get_or_create_connection).
            shoonya = ShoonyaConnection()
            app.state.shoonya_connection = shoonya
            loop = asyncio.get_running_loop()
            connected = await asyncio.wait_for(
                loop.run_in_executor(None, shoonya.connect),
                timeout=SHOONYA_CONNECT_TIMEOUT_SECS
            )
            if connected:
                app.state.shoonya = shoonya
            else:
                # No login here: a restart at any hour used to start one, and
                # failed logins count towards Shoonya's account lockout. The
                # refresh loop below logs in under the login guard's limits.
                app_logger.warning("Shoonya stored token invalid — the refresh loop will auto-login within the allowed window")
        except asyncio.TimeoutError:
            app_logger.warning("Shoonya connect timed out during startup — the refresh loop will log in")
        except Exception as e:
            app_logger.error(f"Shoonya init error: {e}")

        # Always start the refresh loop, even if the connection attempt
        # above failed or raised — it owns every unattended login (see
        # marketengine/shoonyaLoginGuard.py for when it may try).
        shoonya_refresh_task = asyncio.create_task(
            _supervised_background_task(shoonya_daily_refresh, "shoonya_refresh", app)
        )
        top_movers_task = asyncio.create_task(
            _supervised_background_task(top_movers_refresh, "top_movers_refresh", app)
        )

        # ── Option chain: live WS feed + scrip-master daily refresh ──
        # create_market_feeds is also used by
        # ShoonyaConnection.activate_market_feeds after a later login.
        app.state.create_market_feeds = lambda: _create_market_feeds(app)
        if app.state.shoonya is not None:
            _create_market_feeds(app)
        option_master_task = asyncio.create_task(
            _supervised_background_task(option_master_daily_refresh, "option_master_refresh", app)
        )
        future_master_task = asyncio.create_task(
            _supervised_background_task(future_master_daily_refresh, "future_master_refresh", app)
        )
    else:
        app_logger.warning("Shoonya unavailable - market data disabled.")

    # ── Breeze (secondary / stock quotes) ────────────────────────────
    if app.state.shoonya is not None:
        app_logger.info("Breeze skipped (Shoonya is primary).")
    elif _BREEZE_IMPORTABLE:
        app_logger.info("Establishing Breeze session")
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
            app_logger.info("Breeze session ready.")
            refresh_task = asyncio.create_task(
                _supervised_background_task(schedule_daily_refresh, "breeze_refresh", app)
            )
        except asyncio.TimeoutError:
            app_logger.warning("Breeze session establishment timed out — stock quotes will be unavailable")
        except Exception as e:
            app_logger.warning(f"Breeze session failed — stock quotes will be unavailable: {e}")
    else:
        app_logger.info("Breeze unavailable.")

    # ── Equity curve snapshot capture (Postgres-only, no market client) ─
    equity_curve_task = asyncio.create_task(equity_curve_refresh())

    yield

    if refresh_task:
        refresh_task.cancel()
    if stock_symbol_master_task:
        stock_symbol_master_task.cancel()
    if getattr(app.state, "stock_feed_eviction_task", None):
        app.state.stock_feed_eviction_task.cancel()
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
    _shutdown_broker_executor(broker_executor)
    app_logger.info("Server shutting down.")


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
app.include_router(marginRouter)
app.include_router(mutualFundsRouter)
app.include_router(stocksRouter)
app.include_router(searchRouter)
app.include_router(internalRouter)
# SSE (text/event-stream) is excluded from compression by Starlette itself.
app.add_middleware(GZipMiddleware, minimum_size=1000, compresslevel=5)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "https://myproductreact.onrender.com", "https://primepiptrade.com", "https://www.primepiptrade.com"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


latency_logger = logging.getLogger("latency")

# Requests slower than this log at WARNING with a "SLOW" marker:
#   journalctl -u backend.service | grep SLOW
SLOW_REQUEST_THRESHOLD_MS = 1000
# Uptime-monitor pings; logged at DEBUG so they don't drown real traffic.
_QUIET_PATHS = {"/", "/healthz"}


_REQUEST_ID_RX = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
# Only these peers may tell us the real client IP (nginx on the same VM).
_TRUSTED_PROXIES = {"127.0.0.1", "::1"} | {
    ip.strip() for ip in os.getenv("TRUSTED_PROXY_IPS", "").split(",") if ip.strip()
}


def _request_id(request) -> str:
    """Reuses an upstream X-Request-ID only if it is a short safe token, so a
    client can't inject fake fields into log lines; otherwise a new one."""
    incoming = request.headers.get("x-request-id", "")
    return incoming if _REQUEST_ID_RX.match(incoming) else uuid.uuid4().hex[:12]


def _valid_ip(value: str) -> str | None:
    try:
        return str(ipaddress.ip_address(value.strip()))
    except ValueError:
        return None


def _client_ip(request) -> str:
    """Proxy headers are trusted only when the TCP peer is our own proxy.
    nginx's $proxy_add_x_forwarded_for APPENDS the address it saw, so the
    LAST X-Forwarded-For entry is the trustworthy one (earlier entries are
    whatever the client sent)."""
    peer = request.client.host if request.client else ""
    if peer in _TRUSTED_PROXIES:
        forwarded = request.headers.get("x-forwarded-for", "")
        candidates = [forwarded.split(",")[-1]] if forwarded else []
        candidates.append(request.headers.get("x-real-ip", ""))
        for candidate in candidates:
            ip = _valid_ip(candidate)
            if ip:
                return ip
    return _valid_ip(peer) or peer or "-"


@app.middleware("http")
async def add_latency_tracking(request, call_next):
    """One log line per request with its end-to-end time, plus
    Server-Timing / X-Response-Time-Ms / X-Request-ID response headers (the
    browser Network tab shows them). Format:
      rid=<id> <METHOD> <path>[?query] <status> <ms>ms ip=<client>
    INFO normally; WARNING when slow ("SLOW") or 503 ("UNAVAILABLE", a
    dependency like the broker is down); ERROR on other 5xx or an
    unhandled exception. For SSE endpoints the time is until the stream
    opened, not its whole lifetime."""
    request_id = _request_id(request)
    start = time.perf_counter()
    path = request.url.path
    target = f"{path}?{request.url.query}" if request.url.query else path
    try:
        response = await call_next(request)
    except Exception:
        duration_ms = (time.perf_counter() - start) * 1000
        latency_logger.exception(
            f"rid={request_id} {request.method} {target} FAILED {duration_ms:.1f}ms ip={_client_ip(request)}"
        )
        raise

    duration_ms = (time.perf_counter() - start) * 1000
    response.headers["Server-Timing"] = f"app;dur={duration_ms:.1f}"
    response.headers["X-Response-Time-Ms"] = f"{duration_ms:.1f}"
    response.headers["X-Process-Time"] = f"{duration_ms:.1f}"
    response.headers["X-Request-ID"] = request_id

    stream = " stream" if response.headers.get("content-type", "").startswith("text/event-stream") else ""
    log_line = (
        f"rid={request_id} {request.method} {target} {response.status_code} "
        f"{duration_ms:.1f}ms{stream} ip={_client_ip(request)}"
    )
    if response.status_code >= 500 and response.status_code != 503:
        latency_logger.error(log_line)
    elif response.status_code == 503:
        latency_logger.warning(f"UNAVAILABLE {log_line}")
    elif duration_ms >= SLOW_REQUEST_THRESHOLD_MS:
        latency_logger.warning(f"SLOW {log_line}")
    elif path in _QUIET_PATHS:
        latency_logger.debug(log_line)
    else:
        latency_logger.info(log_line)

    return response


@app.middleware("http")
async def add_coop_header(request, call_next):
    response = await call_next(request)
    response.headers["Cross-Origin-Opener-Policy"] = "same-origin-allow-popups"
    return response


@app.api_route("/", methods=["GET", "HEAD"])
def read_root():
    return {"Message": "Finnaly I am able to run my first API"}


@app.api_route("/healthz", methods=["GET", "HEAD"])
async def healthz(request: Request):
    """Liveness for uptime checks / systemd watchdogs: 200 while the process
    serves requests; the market-data fields say whether data is live."""
    shoonya = getattr(request.app.state, "shoonya", None)
    feed = getattr(request.app.state, "option_feed", None)
    return {
        "status": "ok",
        "broker_connected": bool(shoonya is not None and shoonya.is_connected),
        "market_feed_connected": bool(getattr(feed, "is_connected", False)),
    }
