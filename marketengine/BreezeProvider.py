# marketengine/BreezeProvider.py
import asyncio
from typing import List, Callable, Dict, Any
from marketengine.BaseProvider import BaseMarketProvider
from marketengine.config import Config
from decimal import Decimal
from breeze_connect import BreezeConnect
import os
import threading
from dotenv import load_dotenv
from datetime import datetime

load_dotenv()
class BreezeMarketProvider(BaseMarketProvider):
    def __init__(self):
        self.api_key = Config.BREEZE_API_KEY
        self.api_secret = Config.BREEZE_SECRET_KEY
        self.callback = None
        self.symbols = []
        self.breeze = None
        self._async_loop = None

    async def connect(self):
        """
        Initializes the Breeze client and handles the cryptographic daily session validation.
        """
        # Fetch the session token that you cached or input during the morning login flow
        session_token = Config.BREEZE_SESSION_TOKEN 
        
        print("Initializing Secure Breeze API Connection Handler...")
        self.breeze = BreezeConnect(api_key=self.api_key)
        
        # Authenticate session keys with ICICI servers
        self.breeze.generate_session(api_secret=self.api_secret, session_token=session_token)
        
        # Hook into the currently running FastAPI async loop to bridge websocket threads safely
        self._async_loop = asyncio.get_running_loop()
        
        # Connect to Breeze's live streaming WebSocket servers
        
        """def socket_worker():
                print("Connecting to Breeze live streaming WebSocket server on isolated thread...")
                try:
                    #self.breeze.ws_connect()
                except Exception as ex:
                    raise Exception("Exceptionas ex"+ex)    
    # Spin up an isolated background thread for the sync socket connection
        worker_thread = threading.Thread(target=socket_worker, daemon=True)
        worker_thread.start()"""
        
        #self.breeze.ws_connect()
        """today_str = datetime.now().strftime("%Y-%m-%dT")
        response = self.breeze.get_historical_data(
            interval="1minute",
            from_date=f"{today_str}09:15:00.000Z",
            to_date=f"{today_str}15:30:00.000Z",
            stock_code=symbol,
            exchange_code="NSE",
            product_type="cash"
        )"""
        # Assign our internal adapter function to Breeze's raw callback trigger
        self.breeze.on_ticks = self._on_breeze_tick_received
        print("Connected to live Breeze Rate Refresh WebSocket Server.")

    def on_tick(self, callback: Callable[[Dict[str, Any]], None]):
        """
        Registers your decoupled system callback (e.g., your market_repo.save_live_tick method).
        """
        self.callback = callback

    def _on_breeze_tick_received(self, raw_tick: dict):
        """
        Internal sync callback triggered directly by the Breeze SDK thread on every microsecond ticker update.
        """
        if not self.callback or not self._async_loop:
            return

        try:
            # 1. Extract and normalize the raw incoming ICICI feed payload
            symbol = raw_tick.get("stock_code")
            if not symbol:
                return

            # Map exchange fields cleanly to your uniform system architecture standards
            normalized_data = {
                "symbol": symbol,
                "ltp": Decimal(str(raw_tick.get("last", 0.0))),
                "volume": int(raw_tick.get("volume", 0)),
                "timestamp": raw_tick.get("ltt") # Last Traded Time string/epoch
            }

            # 2. Cryptographically safe cross-thread pass:
            # Schedules the async repo task onto your main FastAPI server thread safely.
            asyncio.run_coroutine_threadsafe(
                self.callback(normalized_data), 
                self._async_loop
            )
            
        except Exception as e:
            print(f"Error processing raw Breeze ticker payload stream: {e}")

    async def subscribe(self, symbols: List[str]):
        """
        Subscribes to live feed streams for standard equity instruments on the NSE.
        """
        self.symbols.extend(symbols)
        for symbol in symbols:
            print(f"Sending WebSocket subscription frame for {symbol} (NSE)...")
            
            # Subscribe to the exchange live feed via standard cash product specifications
            self.breeze.subscribe_feeds(
                exchange_code="NSE",
                stock_code=symbol,
                product_type="cash",
                get_exchange_quotes=True,
                get_market_depth=False
            )

    async def disconnect(self):
        """
        Gracefully tears down active stock feeds and closes the socket connections on server shutdown.
        """
        for symbol in self.symbols:
            self.breeze.unsubscribe_feeds(
                exchange_code="NSE",
                stock_code=symbol,
                product_type="cash",
                get_exchange_quotes=True,
                get_market_depth=False
            )
        self.breeze.ws_disconnect()
        print("Breeze streaming channels disconnected safely.")