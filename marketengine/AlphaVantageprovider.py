import asyncio
import aiohttp
from typing import List, Callable, Dict, Any
from marketengine.BaseProvider import BaseMarketProvider
from marketengine.config import Config
from decimal import Decimal
from scripts.simulated_feeder import simulate_market
from datetime import datetime
from breeze_connect import BreezeConnect
class AlphaVantageProvider(BaseMarketProvider):
    def __init__(self):
            self.api_key = Config.BREEZE_API_KEY
            self.api_secret = Config.BREEZE_SECRET_KEY
            self.callback = None
            self.symbols = []
            self.breeze = None
            self._async_loop = None

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
        # Fetch the session token that you cached or input during the morning login flow
        session_token = Config.BREEZE_SESSION_TOKEN 
        
        print("Initializing Secure Breeze API Connection Handler...")
        self.breeze = BreezeConnect(api_key=self.api_key)
        
        # Authenticate session keys with ICICI servers
        self.breeze.generate_session(api_secret=self.api_secret, session_token=session_token)
        self._async_loop = asyncio.get_running_loop()
        while True:
            for symbol in self.symbols:
               
        # Hook into the currently running FastAPI async loop to bridge websocket threads safely
                self._async_loop = asyncio.get_running_loop()
                #url = f"https://www.alphavantage.co/query?function=GLOBAL_QUOTE&symbol={symbol}&apikey={self.api_key}"
                try:
                        SAFE_DATE = "2026-06-17T"
                        response = self.breeze.get_historical_data(
                        interval="1minute",
                        from_date=f"{SAFE_DATE}09:15:00.000Z",
                        to_date=f"{SAFE_DATE}15:30:00.000Z",
                        stock_code=symbol,
                        exchange_code="NSE",
                        product_type="cash"
                    )
                        if response["Status"] == 200:
                            data =  response["Success"]
                            quote = data
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
                                    "stock_code": quote[0].get("stock_code"),
                                    "exchange_code": quote[0].get("exchange_code"),
                                    "timestamp": quote[0].get("datetime"),
                                    "open": float(quote[0].get("open", 0.0)),
                                    "close": float(quote[0].get("close", 0.0)),
                                    "volume": int(quote[0].get("volume", 0))
                                }
                                if self.callback:
                                    await self.callback(normalized_data) 
                                    
                except Exception as e:
                        print(f"Error polling  for {symbol}: {e}")
            
            # Sleep interval to prevent getting rate-limited on free tiers
            await asyncio.sleep(12)