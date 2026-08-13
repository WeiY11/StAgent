"""Cache service package."""

from leagent.services.cache.keys import build_scoped_cache_key
from leagent.services.cache.lru import BoundedLRU, ShardedLRU, make_lru
from leagent.services.cache.observability import (
    CacheStat,
    CacheStatsRegistry,
    get_cache_stats_registry,
    record_cache_event,
    record_cache_set,
)
from leagent.services.cache.redis_backend import RedisCacheBackend, build_redis_clients
from leagent.services.cache.service import (
    CacheService,
    InMemoryCache,
    get_cache_service,
    init_cache_service,
)

__all__ = [
    "CacheService",
    "InMemoryCache",
    "get_cache_service",
    "init_cache_service",
    "CacheStat",
    "CacheStatsRegistry",
    "get_cache_stats_registry",
    "record_cache_event",
    "record_cache_set",
    "BoundedLRU",
    "ShardedLRU",
    "make_lru",
    "build_scoped_cache_key",
    "RedisCacheBackend",
    "build_redis_clients",
]
