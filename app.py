from fastapi import FastAPI,HTTPException
from pydantic import BaseModel
from api.signup import router as signup_router
from api.login import router as login_router
from api.orders import router as orders_router
from api.trade import router as trade_history
from api.portfolio import router as user_portfolio
from contextlib import asynccontextmanager
from scheduler.marketPriceSchedular import MarketPriceScheduler
from database.redisConnection import RedisConnection
from api.Dashboard import router as dashboardRouter
from marketengine.AlphaVantageprovider import AlphaVantageProvider
import asyncio
from repository.MarketRepository import MarketRepository as market_repo

async def on_market_tick_received(tick: dict):
    symbol = tick.get("symbol")
    if symbol:
        # Save straight to our shared memory map component
        payload ={
            "ltp": tick["ltp"],
            "volume": tick["volume"],
            "timestamp": tick["timestamp"]
        }
        
        
        """{
            "ltp": 23560.75,         
            "volume": 4520,           
            "timestamp": 1781811984   
        }"""
        
        await market_repo.save_live_tick(symbol, payload)
        print(f"[Memory Cached] {symbol} -> {tick['ltp']}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    print("App starting... Initializing Market Engine Background Services")
    engine = AlphaVantageProvider()
    await engine.connect()
    engine.on_tick(on_market_tick_received)
    watchlist = ["RELIANCE.BSE", "INFY.BSE", "AAPL"]
    market_task = asyncio.create_task(engine.subscribe(watchlist))
    print("Alpha Vantage async loop successfully scheduled.")
    
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
@app.get("/")
def read_root():
   ##redisConnection=RedisConnection()
    ##redisObject=redisConnection.createRedisConnection()
    ##print(redisObject)
    return {"Message":"Finnaly I am able to run my first API"}





