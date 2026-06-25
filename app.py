import asyncio
from fastapi import FastAPI
from pydantic import BaseModel
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    refresh_task = None
    app.state.breeze = None

    if _BREEZE_IMPORTABLE:
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
            print(f"[WARNING] Breeze session failed — market data endpoints will return 503: {e}")
    else:
        print("App starting... Breeze unavailable, market data endpoints will return 503.")

    yield

    if refresh_task:
        refresh_task.cancel()
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
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "https://myproductreact.onrender.com", "https://primepiptrade.com", "https://www.primepiptrade.com"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def read_root():
    return {"Message": "Finnaly I am able to run my first API"}
