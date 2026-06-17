# scripts/simulated_feeder.py
import asyncio
import random
import time
from repository.MarketRepository import MarketRepository as mk

async def simulate_market():
    print("Starting local market simulator (Alpha Vantage bypass)...")
    symbols = ["NIFTY50", "SENSEX", "RELIANCE.BSE", "INFY.NSE"]
    prices = {"NIFTY50": 23500.0, "SENSEX": 77000.0, "RELIANCE.BSE": 2450.0, "INFY.NSE": 1500.0}
    
    while True:
        for symbol in symbols:
            # Generate a small random price fluctuation (-0.05% to +0.05%)
            change = random.uniform(-0.0005, 0.0005)
            prices[symbol] = round(prices[symbol] * (1 + change), 2)
            
            mock_tick = {
                "symbol": symbol,
                "ltp": prices[symbol],
                "volume": random.randint(1000, 5000),
                "timestamp": int(time.time())
            }
            mk.save_live_tick(symbol,mock_tick)
            # Fire it straight into your production pipeline
            
        await asyncio.sleep(1) # Emit ticks every second
        return mock_tick

"""if __name__ == "__main__":
    asyncio.run(simulate_market())"""