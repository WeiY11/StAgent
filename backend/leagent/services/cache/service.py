"""L1 in-memory cache with an optional JSON-safe Redis L2."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import sys
import time
from typing import TYPE_CHECKING, Any, TypeVar, cast

from leagent.services.base import Service, ServiceType, service_factory
from leagent.services.cache.lru import make_lru
from leagent.services.cache.observability import (
    get_cache_stats_registry,
    record_cache_event,
    record_cache_expiration,
    record_cache_fill,
    record_cache_rejection,
    record_cache_set,
)
from leagent.services.cache.redis_backend import RedisCacheBackend

if TYPE_CHECKING:
    from leagent.config.settings import Settings

logger = logging.getLogger(__name__)

T = TypeVar("T")

DEFAULT_TTL = 3600
MAX_MEMORY_ITEMS = 10000

#: Sentinel distinguishing "key absent" from "value present but falsy". Using it
#: on lookups lets ``None`` / ``0`` / ``""`` / ``[]`` be cached and hit, instead
#: of being treated as a miss on every read.
_MISS: Any = object()

#: Sentinel for "caller did not pass ttl" so we can apply the per-namespace TTL
#: default without colliding with an explicit ``ttl=None`` (never expire).
_UNSET: Any = object()


def _estimate_value_bytes(value: Any) -> int:
    """Best-effort serialized size of *value* for the single-value size guard."""
    try:
        import orjson

        return len(orjson.dumps(value, default=str))
    except Exception:  # noqa: BLE001
        return sys.getsizeof(value)


class InMemoryCache:
    """Async facade over the shared thread-safe LRU primitive.

    Kept ``async`` purely for API compatibility with existing callers; the
    underlying :class:`~leagent.services.cache.lru.BoundedLRU` /
    :class:`ShardedLRU` is synchronous and thread-safe, so no ``asyncio.Lock``
    is needed (and access from threadpool executors is safe too).
    """

    def __init__(self, max_size: int = MAX_MEMORY_ITEMS, *, shards: int = 1) -> None:
        self._lru = make_lru(max_size, shards)

    @property
    def size(self) -> int:
        return self._lru.size

    async def get(self, key: str, default: Any = None) -> Any | None:
        value, _hit = self._lru.get(key, default)
        return value

    async def set(self, key: str, value: Any, ttl: float | None = None) -> int:
        """Insert *key*; returns the number of entries evicted for capacity."""
        return self._lru.set(key, value, ttl)

    async def update(
        self,
        key: str,
        updater: Any,
        *,
        default: Any = None,
        ttl: float | None = None,
    ) -> tuple[Any, int]:
        """Atomic read-modify-write over the shared primitive."""
        return self._lru.update(key, updater, default=default, ttl=ttl)

    async def sweep_expired(self) -> int:
        """Proactively drop expired entries; returns the count removed."""
        return self._lru.sweep_expired()

    async def delete(self, key: str) -> bool:
        return self._lru.delete(key)

    async def exists(self, key: str) -> bool:
        return self._lru.exists(key)

    async def clear(self) -> None:
        self._lru.clear()

    async def keys(self, pattern: str = "*") -> list[str]:
        return self._lru.keys(pattern)

    async def delete_prefix(self, prefix: str) -> int:
        """Delete every key starting with *prefix* in a single locked pass."""
        return self._lru.delete_prefix(prefix)

    def resize(self, max_size: int) -> int:
        return self._lru.resize(max_size)

    @property
    def stats(self) -> dict[str, Any]:
        st = self._lru.stats
        return {
            "size": st["size"],
            "max_size": st["max_size"],
            "hits": st["hits"],
            "misses": st["misses"],
            "evictions": st["lru_evictions"],
            "expirations": st["expirations"],
            "hit_rate": st["hit_rate"],
        }


@service_factory(ServiceType.CACHE)
class CacheService(Service):
    """In-memory LRU cache service (no Redis)."""

    def __init__(
        self,
        settings: Settings,
        *,
        redis_client: Any | None = None,
        redis_prefix: str | None = None,
    ) -> None:
        super().__init__(settings)
        self._cache_cfg = settings.cache
        self._memory_cache = InMemoryCache(
            max_size=self._cache_cfg.max_items,
            shards=self._cache_cfg.shard_count,
        )
        self._prefix = f"{settings.app_name}:"
        configured_redis = getattr(settings, "redis", None)
        configured_prefix = redis_prefix or getattr(configured_redis, "prefix", None) or "leagent"
        self._redis_backend = (
            RedisCacheBackend(
                redis_client,
                prefix=str(configured_prefix),
            )
            if redis_client is not None
            else None
        )
        self._redis_healthy = False
        # Single-flight state: one in-flight fill task per full key coalesces
        # concurrent ``get_or_set`` callers onto a single factory execution.
        self._inflight: dict[str, asyncio.Task[Any]] = {}
        self._inflight_waiters: dict[str, int] = {}
        self._inflight_lock = asyncio.Lock()
        self._maintenance_stop = asyncio.Event()
        self._maintenance_task: asyncio.Task[None] | None = None
        registry = get_cache_stats_registry()
        registry.register_size_provider(
            "cache:_memory", lambda: self._memory_cache.size
        )
        # Pull-based diagnostics: split LRU/TTL evictions + capacity are only
        # materialized when a snapshot is taken, never on the hot path.
        registry.register_detail_provider(
            "cache:_memory", lambda: self._memory_cache.stats
        )

    @property
    def name(self) -> str:
        return "CacheService"

    @property
    def using_redis(self) -> bool:
        return self._redis_backend is not None and self._redis_healthy

    @staticmethod
    def _stat_name(namespace: str | None) -> str:
        return f"cache:{namespace or 'default'}"

    def memory_stats(self) -> dict[str, Any]:
        """Live LRU stats (size, hits, misses, evictions, hit_rate)."""
        return self._memory_cache.stats

    def _make_key(self, key: str, namespace: str | None = None) -> str:
        if namespace:
            return f"{self._prefix}{namespace}:{key}"
        return f"{self._prefix}{key}"

    async def _do_start(self) -> None:
        if self._redis_backend is not None:
            try:
                await self._redis_backend.ping()
            except Exception as exc:
                raise RuntimeError("Configured Redis cache is unavailable") from exc
            self._redis_healthy = True
            logger.info("CacheService: memory L1 + Redis L2")
        else:
            logger.info("CacheService: in-memory cache only")
        self._maintenance_stop.clear()
        if self._cache_cfg.maintenance_interval_sec > 0:
            self._maintenance_task = asyncio.create_task(self._maintenance_loop())

    async def _do_stop(self) -> None:
        self._maintenance_stop.set()
        if self._maintenance_task is not None:
            await self._maintenance_task
            self._maintenance_task = None
        await self._memory_cache.clear()
        if self._redis_backend is not None:
            await self._redis_backend.close()
        self._redis_healthy = False

    async def _maintenance_loop(self) -> None:
        """Periodically sweep expired entries so TTL memory is reclaimed.

        Waits on the stop event with a timeout so shutdown is immediate; the
        sweep only runs when the wait times out (i.e. no stop requested).
        """
        interval = self._cache_cfg.maintenance_interval_sec
        while interval > 0 and not self._maintenance_stop.is_set():
            try:
                await asyncio.wait_for(
                    self._maintenance_stop.wait(), timeout=interval
                )
            except TimeoutError:
                expired = await self._memory_cache.sweep_expired()
                if expired:
                    record_cache_expiration("cache:_memory", count=expired)

    async def _do_health_check(self) -> dict[str, Any]:
        if self._redis_backend is None:
            return {
                "backend": "memory",
                "memory_stats": self._memory_cache.stats,
            }
        try:
            await self._redis_backend.ping()
            self._redis_healthy = True
        except Exception:
            self._redis_healthy = False
            logger.warning("Redis cache health check failed", exc_info=True)
        return {
            "backend": "memory+redis",
            "redis_healthy": self._redis_healthy,
            "memory_stats": self._memory_cache.stats,
        }

    def _disable_redis(self, operation: str) -> None:
        """Fail individual cache operations over to L1 without exposing config."""
        self._redis_healthy = False
        logger.warning("Redis L2 %s failed; serving process-local cache", operation, exc_info=True)

    async def get(self, key: str, *, namespace: str | None = None, default: T | None = None) -> T | None:
        full_key = self._make_key(key, namespace)
        result = await self._memory_cache.get(full_key, _MISS)
        stat_name = self._stat_name(namespace)
        if result is _MISS:
            if self.using_redis and self._redis_backend is not None:
                try:
                    remote_hit, remote_value = await self._redis_backend.get(full_key)
                except Exception:
                    self._disable_redis("get")
                else:
                    if remote_hit:
                        evicted = await self._memory_cache.set(
                            full_key,
                            remote_value,
                            self._effective_ttl(
                                full_key,
                                self._cache_cfg.ttl_for(namespace),
                            ),
                        )
                        record_cache_set(stat_name, evicted=evicted)
                        record_cache_event(stat_name, hit=True, entries=self._memory_cache.size)
                        return cast("T", remote_value)
            record_cache_event(stat_name, hit=False, entries=self._memory_cache.size)
            return default
        record_cache_event(stat_name, hit=True, entries=self._memory_cache.size)
        return cast("T", result)

    async def set(self, key: str, value: Any, *, namespace: str | None = None, ttl: Any = _UNSET) -> bool:
        if ttl is _UNSET:
            ttl = self._cache_cfg.ttl_for(namespace)
        full_key = self._make_key(key, namespace)
        stat_name = self._stat_name(namespace)
        # Reject oversized single values: a few large payloads must not evict
        # the whole working set. Callers cache a FileRef/MediaRef instead.
        value_bytes = _estimate_value_bytes(value)
        if value_bytes > self._cache_cfg.max_value_bytes:
            record_cache_rejection(stat_name, reason="oversized")
            return False
        effective_ttl = self._effective_ttl(full_key, ttl)
        evicted = await self._memory_cache.set(full_key, value, effective_ttl)
        record_cache_set(stat_name, evicted=evicted)
        if self.using_redis and self._redis_backend is not None:
            try:
                replicated = await self._redis_backend.set(
                    full_key,
                    value,
                    ttl=effective_ttl,
                )
                if not replicated:
                    logger.debug("Redis L2 skipped a non-JSON cache value")
            except Exception:
                self._disable_redis("set")
        return True

    def _effective_ttl(self, full_key: str, ttl: float | None) -> float | None:
        """Apply deterministic +/- jitter to *ttl* keyed by *full_key*.

        Deterministic (hash-derived) jitter keeps tests repeatable and prevents
        a namespace's entries from all expiring in one burst. ``None`` /
        non-positive TTLs and a zero jitter ratio pass through unchanged.
        """
        if ttl is None or ttl <= 0 or self._cache_cfg.ttl_jitter_ratio <= 0:
            return ttl
        raw = int(hashlib.sha256(full_key.encode("utf-8")).hexdigest()[:8], 16)
        unit = raw / 0xFFFFFFFF
        ratio = self._cache_cfg.ttl_jitter_ratio
        return ttl * (1.0 - ratio + (2.0 * ratio * unit))

    async def delete(self, key: str, *, namespace: str | None = None) -> bool:
        full_key = self._make_key(key, namespace)
        memory_deleted = await self._memory_cache.delete(full_key)
        redis_deleted = False
        if self.using_redis and self._redis_backend is not None:
            try:
                redis_deleted = await self._redis_backend.delete(full_key)
            except Exception:
                self._disable_redis("delete")
        return memory_deleted or redis_deleted

    async def delete_prefix(self, prefix: str, *, namespace: str | None = None) -> int:
        """Delete all keys whose logical key starts with *prefix* in *namespace*.

        Single locked pass over the store instead of an O(n) fetch-then-delete
        loop, so bulk invalidation stays cheap even for large namespaces.
        """
        full_prefix = self._make_key(prefix, namespace)
        memory_removed = await self._memory_cache.delete_prefix(full_prefix)
        redis_removed = 0
        if self.using_redis and self._redis_backend is not None:
            try:
                redis_removed = await self._redis_backend.delete_prefix(full_prefix)
            except Exception:
                self._disable_redis("delete_prefix")
        return max(memory_removed, redis_removed)

    async def exists(self, key: str, *, namespace: str | None = None) -> bool:
        full_key = self._make_key(key, namespace)
        if await self._memory_cache.exists(full_key):
            return True
        if self.using_redis and self._redis_backend is not None:
            try:
                return await self._redis_backend.exists(full_key)
            except Exception:
                self._disable_redis("exists")
        return False

    async def get_or_set(self, key: str, factory: Any, *, namespace: str | None = None, ttl: Any = _UNSET) -> Any:
        # Sentinel default distinguishes a cached falsy value from a miss, so a
        # cached ``0`` / ``""`` / ``[]`` is returned instead of re-running the
        # factory on every call.
        cached = await self.get(key, namespace=namespace, default=_MISS)
        if cached is not _MISS:
            return cached
        full_key = self._make_key(key, namespace)

        async with self._inflight_lock:
            task = self._inflight.get(full_key)
            if task is None:
                task = asyncio.create_task(
                    self._fill(key, factory, namespace=namespace, ttl=ttl, full_key=full_key)
                )
                self._inflight[full_key] = task
            else:
                # This caller piggy-backs on the in-flight fill instead of
                # running its own factory; count it so single-flight savings
                # are observable.
                self._inflight_waiters[full_key] = (
                    self._inflight_waiters.get(full_key, 0) + 1
                )

        try:
            # Shield so a cancelled waiter never cancels the shared fill; other
            # coalesced callers still receive the result.
            return await asyncio.shield(task)
        finally:
            if task.done():
                async with self._inflight_lock:
                    if self._inflight.get(full_key) is task:
                        self._inflight.pop(full_key, None)

    async def _fill(
        self, key: str, factory: Any, *, namespace: str | None, ttl: Any, full_key: str
    ) -> Any:
        # Re-check under the fill: a concurrent writer may have populated the
        # key between the miss and task creation.
        cached = await self.get(key, namespace=namespace, default=_MISS)
        if cached is not _MISS:
            return cached
        started = time.perf_counter()
        value = await factory() if asyncio.iscoroutinefunction(factory) else factory()
        if asyncio.iscoroutine(value):
            value = await value
        await self.set(key, value, namespace=namespace, ttl=ttl)
        duration = time.perf_counter() - started
        async with self._inflight_lock:
            coalesced = self._inflight_waiters.pop(full_key, 0)
        record_cache_fill(
            self._stat_name(namespace), duration_sec=duration, coalesced=coalesced
        )
        return value

    async def get_generation(self, subject: str, *, namespace: str) -> int:
        """Return the current data generation for *subject* (0 when unset)."""
        full_key = self._make_key(f"__generation__:{subject}", namespace)
        # Generation values participate in cache keys, so in distributed mode
        # they must consult the shared counter before a process-local L1 copy.
        if self.using_redis and self._redis_backend is not None:
            try:
                remote_hit, remote_value = await self._redis_backend.get(full_key)
                if remote_hit:
                    value = int(remote_value or 0)
                    await self._memory_cache.set(full_key, value, None)
                    return value
                return 0
            except (TypeError, ValueError):
                return 0
            except Exception:
                self._disable_redis("get_generation")
        local_value = await self._memory_cache.get(full_key, 0)
        return int(local_value or 0)

    async def bump_generation(self, subject: str, *, namespace: str) -> int:
        """Atomically increment and return the data generation for *subject*.

        Callers bump this only after a durable mutation commits; readers fold
        the generation into their cache key so stale entries are never hit.
        """
        full_key = self._make_key(f"__generation__:{subject}", namespace)
        if self.using_redis and self._redis_backend is not None:
            try:
                value = await self._redis_backend.increment(full_key, 1, ttl=None)
                evicted = await self._memory_cache.set(full_key, value, None)
                record_cache_set(self._stat_name(namespace), evicted=evicted)
                return value
            except Exception:
                self._disable_redis("bump_generation")
        value, evicted = await self._memory_cache.update(
            full_key,
            lambda current: int(current or 0) + 1,
            default=0,
            ttl=None,
        )
        record_cache_set(self._stat_name(namespace), evicted=evicted)
        return int(value)

    async def clear_namespace(self, namespace: str) -> int:
        """Drop every key under *namespace* in a single locked pass."""
        return await self.delete_prefix("", namespace=namespace)

    async def increment(
        self,
        key: str,
        amount: int = 1,
        *,
        namespace: str | None = None,
        ttl: Any = _UNSET,
    ) -> int:
        """Atomically add *amount* to a counter and return the new value.

        Routes through the atomic primitive ``update`` so the read-modify-write
        cannot lose concurrent increments, and the write inherits the
        per-namespace TTL default when *ttl* is omitted — a bare ``increment`` no
        longer creates a never-expiring, unobserved counter.
        """
        if ttl is _UNSET:
            ttl = self._cache_cfg.ttl_for(namespace)
        full_key = self._make_key(key, namespace)
        effective_ttl = self._effective_ttl(full_key, ttl)
        if self.using_redis and self._redis_backend is not None:
            try:
                value = await self._redis_backend.increment(
                    full_key,
                    amount,
                    ttl=effective_ttl,
                )
                evicted = await self._memory_cache.set(full_key, value, effective_ttl)
                record_cache_set(self._stat_name(namespace), evicted=evicted)
                return value
            except Exception:
                self._disable_redis("increment")
        value, evicted = await self._memory_cache.update(
            full_key,
            lambda current: int(current or 0) + amount,
            default=0,
            ttl=effective_ttl,
        )
        record_cache_set(self._stat_name(namespace), evicted=evicted)
        return int(value)

    async def get_many(self, keys: list[str], *, namespace: str | None = None) -> dict[str, Any]:
        result = {}
        for key in keys:
            value = await self.get(key, namespace=namespace, default=_MISS)
            if value is not _MISS:
                result[key] = value
        return result

    async def set_many(self, mapping: dict[str, Any], *, namespace: str | None = None, ttl: Any = _UNSET) -> bool:
        for key, value in mapping.items():
            await self.set(key, value, namespace=namespace, ttl=ttl)
        return True

    async def warm(
        self,
        mapping: dict[str, Any],
        *,
        namespace: str | None = None,
        ttl: Any = _UNSET,
    ) -> int:
        """Preload *mapping* into *namespace* (cache warming).

        Intended for startup / post-deploy priming of hot, expensive-to-build
        entries so the first real request hits instead of missing. Returns the
        number of keys written.
        """
        await self.set_many(mapping, namespace=namespace, ttl=ttl)
        return len(mapping)

    def update_limits(self, *, max_items: int | None = None) -> dict[str, Any]:
        """Resize the in-memory store at runtime (Admin / autotuning lever).

        Shrinking evicts LRU entries immediately. Rejects a ``max_items`` below
        the configured ``shard_count`` (which would starve one or more shards)
        with :class:`ValueError`. Returns the effective config snapshot so
        callers can echo the applied value.
        """
        if max_items is not None:
            if int(max_items) < self._cache_cfg.shard_count:
                raise ValueError(
                    "cache max_items must be >= shard_count "
                    f"({self._cache_cfg.shard_count})"
                )
            self._memory_cache.resize(max_items)
            self._cache_cfg.max_items = int(max_items)
        return self.config_snapshot()

    def config_snapshot(self) -> dict[str, Any]:
        """Return the live cache configuration (for the stats endpoint).

        ``backend``/``persistent`` distinguish the always-present local L1
        from an active shared Redis L2, so a degraded runtime is never
        misreported as distributed.
        """
        cfg = self._cache_cfg
        return {
            "backend": "memory+redis" if self.using_redis else "memory",
            "persistent": self.using_redis,
            "redis_configured": self._redis_backend is not None,
            "default_ttl": cfg.default_ttl,
            "max_items": cfg.max_items,
            "max_value_bytes": cfg.max_value_bytes,
            "shard_count": cfg.shard_count,
            "namespace_ttls": dict(cfg.namespace_ttls),
            "resident_items": self._memory_cache.size,
        }


_cache_service: CacheService | None = None


def get_cache_service() -> CacheService:
    if _cache_service is None:
        raise RuntimeError("CacheService not initialized")
    return _cache_service


async def init_cache_service(
    settings: Settings,
    *,
    redis_client: Any | None = None,
    redis_prefix: str | None = None,
) -> CacheService:
    global _cache_service
    _cache_service = CacheService(
        settings,
        redis_client=redis_client,
        redis_prefix=redis_prefix,
    )
    await _cache_service.start()
    return _cache_service
