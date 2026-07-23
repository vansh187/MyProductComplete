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
    _sync_redis_client: redis.Redis = None

    @classmethod
    async def createRedisConnection(cls) -> aioredis.Redis:
        """
        Production-Ready: Lazily initializes and returns a secure, non-blocking 
        Redis client instance that manages its own warm internal connection pool.
        """
        if cls._redis_client is None:
            print(f"Establishing secure connection to cloud Redis: {os.getenv('REDIS_HOST_URL')}")
            
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
                health_check_interval=30,                     # Ping the database in the background every 30s to keep connection hot
                max_connections=200                           # Default (100) can exhaust under a tick burst on a popular
                                                                # instrument with many open positions - each tick briefly
                                                                # holds several pooled connections (lock + hget + hset)
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

    @classmethod
    def get_sync_connection(cls) -> redis.Redis:
        """
        Blocking counterpart to createRedisConnection(), for call sites that
        run outside the asyncio event loop - e.g. order execution, which runs
        inside a plain psycopg2 transaction on FastAPI's sync threadpool.
        Mixing that sync code with the asyncio client above would bind its
        connections to whatever short-lived event loop last touched it and
        break on the next call from a different thread, so this keeps a
        separate, thread-safe blocking client with its own pool instead.
        """
        if cls._sync_redis_client is None:
            cls._sync_redis_client = redis.Redis(
                host=os.getenv("REDIS_HOST"),
                port=int(os.getenv("REDIS_PORT", 6379)),
                password=os.getenv("REDIS_PASSWORD"),
                decode_responses=True,
                socket_timeout=5.0,
                socket_keepalive=True,
                retry_on_timeout=True,
                health_check_interval=30,
            )
        return cls._sync_redis_client

    @classmethod
    def close_sync_connection(cls):
        """Closes the blocking client's pool during hot-reloads or shutdown."""
        if cls._sync_redis_client is not None:
            print("Closing production sync Redis client connection pool cleanly.")
            cls._sync_redis_client.close()
            cls._sync_redis_client = None

