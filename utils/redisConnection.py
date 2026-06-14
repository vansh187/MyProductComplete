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
            host="megasafe-dreamy-inerrant-47439.db.redis.io",
            port=15947,
            password="redisProductDatabase@1234",
            decode_responses=True
        )
            return redis_client
   
   
   
if __name__== "main":
   redisConnection=RedisConnection()
   redisConnection.createRedisConnection(redisConnection)