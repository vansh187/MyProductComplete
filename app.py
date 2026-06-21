from fastapi import FastAPI,HTTPException
from pydantic import BaseModel
from api.signup import router as signup_router
from api.login import router as login_router
from api.orders import router as orders_router
from api.trade import router as trade_history
from api.portfolio import router as user_portfolio
from api.VerifyFundTransaction import router as verify_transaction
from contextlib import asynccontextmanager
from scheduler.marketPriceSchedular import MarketPriceScheduler
#from database.redisConnection import RedisConnection
from api.Dashboard import router as dashboardRouter
from marketengine.AlphaVantageprovider import AlphaVantageProvider
from marketengine.BreezeProvider import BreezeMarketProvider
import asyncio
from repository.MarketRepository import MarketRepository as market_repo
from api.AddfundstoWallet import router as razorPayPaymentRouter
from fastapi.middleware.cors import CORSMiddleware


async def on_market_tick_received(tick: dict):
    symbol = tick.get("stock_code")
    if symbol:
        # Save straight to our shared memory map component
        payload ={
            "exchange_code": tick["exchange_code"],
            "volume": tick["volume"],
            "open" : tick["open"],
            "close" : tick["close"],
            "timestamp": tick["timestamp"]
        }
        
        
        """{
            "ltp": 23560.75,         
            "volume": 4520,           
            "timestamp": 1781811984   
        }"""
        
        await market_repo.save_live_tick(symbol, payload)
        print(f"[Memory Cached] {symbol} -> {tick['exchange_code']}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    print("App starting... Initializing Market Engine Background Services")
    engine = AlphaVantageProvider()
    await engine.connect()
    # 1. Register your internal adapter method reference cleanly
    engine.on_tick(on_market_tick_received)

    watchlist = ["RELIND", "INFTEC","TANCAM"]

    market_task = asyncio.create_task(engine.subscribe(watchlist))
    
    yield
    print("Terminating background HTTP data sessions...")
    if engine._session and not engine._session.closed:
        await engine._session.close()
    
    try:
        # Wait for the task to acknowledge the cancellation cleanly
        await market_task
    except asyncio.CancelledError:
        print("Background market task safely terminated.")
    except Exception as e:
        print(f"Unexpected error while tearing down engine: {e}")
        
    print("Cleanup complete. Server offline.")
    
    """#MarketPriceScheduler.start()
    print("App starting... Scheduler initialized")
    yield
    # ---- shutdown ----
    print("App shutting down...")"""

app=FastAPI(lifespan=lifespan)
app.include_router(orders_router)
app.include_router(signup_router)
app.include_router(login_router)
app.include_router(trade_history)
app.include_router(user_portfolio)
app.include_router(dashboardRouter)
app.include_router(razorPayPaymentRouter)
app.include_router(verify_transaction)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173","https://myproductreact.onrender.com/","https://primepiptrade.com/"], # Your React dev server URL
    allow_credentials=True,
    allow_methods=["*"], # Allows all HTTP methods (GET, POST, etc.)
    allow_headers=["*"], # Allows all headers
)
@app.get("/")
def read_root():
   ##redisConnection=RedisConnection()
    ##redisObject=redisConnection.createRedisConnection()
    ##print(redisObject)
    return {"Message":"Finnaly I am able to run my first API"}





