import redis
from runtime_settings import redis_config

r = redis.Redis(**redis_config())
