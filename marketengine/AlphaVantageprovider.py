import asyncio
import aiohttp
from typing import List, Callable, Dict, Any
from marketengine.BaseProvider import BaseMarketProvider
from marketengine.config import Config
from decimal import Decimal
from scripts.simulated_feeder import simulate_market
class AlphaVantageProvider(BaseMarketProvider):
    def __init__(self):
        self.api_key = Config.ALPHA_VANTAGE_KEY
        self.callback = None
        self.symbols = []
        self._session = None

    async def connect(self):
        self._session = aiohttp.ClientSession()
        print("Connected to Alpha Vantage Engine Session.")

    async def subscribe(self, symbols: List[str]):
        self.symbols.extend(symbols)
        # Spin up background async worker loops for polling
        asyncio.create_task(self._start_feed_loop())

    def on_tick(self, callback: Callable[[Dict[str, Any]], None]):
        self.callback = callback

    async def _start_feed_loop(self):
        while True:
            for symbol in self.symbols:
                url = f"https://www.alphavantage.co/query?function=GLOBAL_QUOTE&symbol={symbol}&apikey={self.api_key}"
                try:
                    async with self._session.get(url) as response:
                        if response.status == 200:
                            data = await response.json()
                            quote = data.get("Global Quote", {})
                            """await self.callback(
                                {
                                    "ltp": 23560.75,         
                                    "volume": 4520,          
                                    "timestamp": 1781811984   
                                }
                            ) """
                            if quote:
                                # Normalizing structure to match your platform standards
                                normalized_data = {
                                    "symbol": quote.get("01. symbol"),
                                    "ltp": Decimal(quote.get("05. price", 0.0)),
                                    "volume": int(quote.get("06. volume", 0)),
                                    "timestamp": quote.get("07. latest trading day")
                                }
                                if self.callback:
                                    await self.callback(normalized_data) 
                                    
                except Exception as e:
                    print(f"Error polling Alpha Vantage for {symbol}: {e}")
            
            # Sleep interval to prevent getting rate-limited on free tiers
            await asyncio.sleep(12)