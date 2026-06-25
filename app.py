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
from breeze_connect import BreezeConnect
from marketengine.config import Config
from marketengine.BreezeSessionManager import schedule_daily_refresh


@asynccontextmanager
async def lifespan(app: FastAPI):
    print("App starting... Establishing Breeze session")
    breeze = BreezeConnect(api_key=Config.BREEZE_API_KEY)
    breeze.generate_session(
        api_secret=Config.BREEZE_SECRET_KEY,
        session_token=Config.BREEZE_SESSION_TOKEN
    )
    app.state.breeze = breeze
    print("Breeze session ready.")

    # Daily background task: re-reads .env at 8:45 AM IST and refreshes session
    refresh_task = asyncio.create_task(schedule_daily_refresh(app))

    yield

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
