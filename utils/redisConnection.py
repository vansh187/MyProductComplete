import redis
import os
from dotenv import load_dotenv
load_dotenv()
class RedisConnection:
    redisclient=None
    def __init__(self):
       pass


    def createRedisConnection(self):
            redis_client = redis.Redis(
            host=os.getenv("REDIS_HOST"),
            port=int(os.getenv("REDIS_PORT", "6379")),
            password=os.getenv("REDIS_PASSWORD"),
            decode_responses=True
        )
            return redis_client
   
   
   
if __name__== "main":
   redisConnection=RedisConnection()
   redisConnection.createRedisConnection(redisConnection)