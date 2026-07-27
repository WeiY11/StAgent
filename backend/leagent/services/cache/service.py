"""Cache service — in-memory LRU only."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import sys
import time
from typing import TYPE_CHECKING, Any, TypeVar

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

    async def set(self, key: str, value: Any, ttl: int | None = None) -> int:
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

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self._cache_cfg = settings.cache
        self._memory_cache = InMemoryCache(
            max_size=self._cache_cfg.max_items,
            shards=self._cache_cfg.shard_count,
        )
        self._prefix = f"{settings.app_name}:"
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
        return False

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
        return {
            "backend": "memory",
            "memory_stats": self._memory_cache.stats,
        }

    async def get(self, key: str, *, namespace: str | None = None, default: T | None = None) -> T | None:
        full_key = self._make_key(key, namespace)
        result = await self._memory_cache.get(full_key, _MISS)
        stat_name = self._stat_name(namespace)
        if result is _MISS:
            record_cache_event(stat_name, hit=False, entries=self._memory_cache.size)
            return default
        record_cache_event(stat_name, hit=True, entries=self._memory_cache.size)
        return result

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
        evicted = await self._memory_cache.set(
            full_key, value, self._effective_ttl(full_key, ttl)
        )
        record_cache_set(stat_name, evicted=evicted)
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
        return await self._memory_cache.delete(full_key)

    async def delete_prefix(self, prefix: str, *, namespace: str | None = None) -> int:
        """Delete all keys whose logical key starts with *prefix* in *namespace*.

        Single locked pass over the store instead of an O(n) fetch-then-delete
        loop, so bulk invalidation stays cheap even for large namespaces.
        """
        full_prefix = self._make_key(prefix, namespace)
        return await self._memory_cache.delete_prefix(full_prefix)

    async def exists(self, key: str, *, namespace: str | None = None) -> bool:
        full_key = self._make_key(key, namespace)
        return await self._memory_cache.exists(full_key)

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
        value = await self.get(
            f"__generation__:{subject}", namespace=namespace, default=0
        )
        return int(value or 0)

    async def bump_generation(self, subject: str, *, namespace: str) -> int:
        """Atomically increment and return the data generation for *subject*.

        Callers bump this only after a durable mutation commits; readers fold
        the generation into their cache key so stale entries are never hit.
        """
        full_key = self._make_key(f"__generation__:{subject}", namespace)
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
        full_prefix = f"{self._prefix}{namespace}:"
        return await self._memory_cache.delete_prefix(full_prefix)

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
        value, evicted = await self._memory_cache.update(
            full_key,
            lambda current: int(current or 0) + amount,
            default=0,
            ttl=self._effective_ttl(full_key, ttl),
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

        ``backend``/``persistent`` are reported explicitly so operators never
        assume a durable (e.g. Redis) cache exists behind the API: the runtime
        cache is process-local, in-memory, and lost on restart.
        """
        cfg = self._cache_cfg
        return {
            "backend": "memory",
            "persistent": False,
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


async def init_cache_service(settings: Settings) -> CacheService:
    global _cache_service
    _cache_service = CacheService(settings)
    await _cache_service.start()
    return _cache_service
