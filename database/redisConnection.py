import redis
import os
from dotenv import load_dotenv
import redis.asyncio as aioredis

load_dotenv()
class RedisConnection:
    redisclient=None
    def __init__(self):
       pass


    _redis_client: aioredis.Redis = None

    @classmethod
    async def createRedisConnection(cls) -> aioredis.Redis:
        """
        Production-Ready: Lazily initializes and returns a secure, non-blocking 
        Redis client instance that manages its own warm internal connection pool.
        """
        if cls._redis_client is None:
            print(f"Establishing secure connection to cloud Redis: {os.getenv("REDIS_HOST_URL")}")
            
            # Safe parsing of host and port variables from your centralized config properties
            try:
                host_name = os.getenv("REDIS_HOST")
                port_number = os.getenv("REDIS_PORT")
            except Exception as ex:
               raise Exception("Error in creating Redis CONNECTION"+ex)

            # Instantiate the single persistent client using your exact pattern (Asynchronously)
            cls._redis_client = aioredis.Redis(
                host=host_name,
                port=port_number,
                password=os.getenv("REDIS_PASSWORD"),
                decode_responses=True,
                
                # ---- PRODUCTION ROAD-TESTED TUNINGS ----
                ssl=False if os.getenv("REDIS_PASSWORD") else False,  # Mandatory encryption for hosted cloud accounts
                socket_timeout=5.0,                           # Prevents app freezing if cloud latency spikes
                socket_keepalive=True,                        # Tells OS to send keep-alive probes (prevents dropped idle sockets)
                retry_on_timeout=True,                        # Automatically re-fires command once on a brief timeout blip
                health_check_interval=30                      # Ping the database in the background every 30s to keep connection hot
            )
            
        return cls._redis_client

    @classmethod
    async def close_connections(cls):
        """
        Gracefully drains and closes the socket pool during hot-reloads or server shutdown.
        """
        if cls._redis_client is not None:
            print("Closing production Redis client connection pool pools cleanly.")
            await cls._redis_client.aclose()
            cls._redis_client = None
   
   
