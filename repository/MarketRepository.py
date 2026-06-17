import json
from database.redisConnection import RedisConnection


class MarketRepository:

    def __init__(self):
        pass
    
    
    async def save_live_tick( symbol: str, payload: dict) -> bool:
        """Professional Instance Method"""
        try:
            factoryConnection=RedisConnection()
            db = await factoryConnection.createRedisConnection()
            await db.set(f"market:{symbol}", json.dumps(payload))
            return True
        except Exception as e:
            print(f"Failed to save tick for {symbol}: {e}")
            return False
        
        
    async def get_live_tick(self, symbol: str) -> dict | None:
        """Professional Instance Method"""
        try:
            factoryConnection=RedisConnection()
            db = await factoryConnection.createRedisConnection()
            raw_data = await db.get(f"market:{symbol}")
            return json.loads(raw_data) if raw_data else None
        except Exception as e:
            print(f"Failed to read tick for {symbol}: {e}")
            return None
        
    async def append_historical_candle(self, symbol: str, interval: str, candle_data: dict):
        """
        2. NEW: Appends a completed OHLCV candle to a Sorted Set.
        candle_data format: {"o": 2450.0, "h": 2465.0, "l": 2445.0, "c": 2460.0, "v": 12000, "t": 1718668800}
        """
        try:
            db = await self.factory.createRedisConnection()
            key = f"market:candles:{interval}:{symbol}"
            timestamp = candle_data["t"]
            
            # Store in a sorted set using timestamp as the score
            await db.zadd(key, {json.dumps(candle_data): timestamp})
            
            # Optimization Guardrail: Keep only the last 500 candles to save cloud RAM memory
            await db.zremrangebyrank(key, 0, -501)
        except Exception as e:
            print(f"Failed to append candle for {symbol}: {e}")

    async def get_historical_candles(self, symbol: str, interval: str, count: int = 100) -> list:
        """
        3. 📊 NEW: Fetches the last 'N' candles for your chart integration UI
        """
        try:
            db = await self.factory.createRedisConnection()
            key = f"market:candles:{interval}:{symbol}"
            
            # Fetch the latest 'count' items from the sorted set
            raw_candles = await db.zrevrange(key, 0, count - 1)
            
            # Reverse them back so they are in chronological order for the UI chart
            candles = [json.loads(c) for c in raw_candles]
            candles.reverse()
            return candles
        except Exception as e:
            print(f"Error fetching candles: {e}")
            return []    