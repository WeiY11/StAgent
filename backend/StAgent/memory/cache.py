"""Tiered caching layer for the agent memory stack.

Provides L1 (in-process LRU) and L2 (Redis) caching for embeddings,
recall results, and memory metadata. Eliminates redundant embedding
API calls and store lookups when the same memories are accessed
across turns.
"""

from __future__ import annotations

import hashlib
import logging
import time
from typing import TYPE_CHECKING, Any

from StAgent.services.cache.lru import BoundedLRU
from StAgent.services.cache.observability import (
    get_cache_stats_registry,
    record_cache_event,
    record_cache_set,
)

if TYPE_CHECKING:
    from StAgent.services.cache.service import CacheService

logger = logging.getLogger(__name__)

_DEFAULT_L1_SIZE = 512
_DEFAULT_L2_TTL = 3600

#: L2 circuit-breaker tuning. After this many consecutive L2 (Redis / shared
#: CacheService) failures the breaker opens and L2 is skipped for the cooldown
#: window, so a flapping L2 never repeatedly stalls the hot path — the tier
#: degrades cleanly to L1-only until L2 recovers.
_L2_FAIL_THRESHOLD = 5
_L2_COOLDOWN_SECONDS = 30.0


def _resolve_l1_size(explicit: int | None, settings_attr: str, fallback: int) -> int:
    """Resolve an L1 bound: explicit arg > settings > hardcoded fallback."""
    if explicit is not None:
        return explicit
    try:
        from StAgent.config.settings import get_settings

        return int(getattr(get_settings().cache, settings_attr))
    except Exception:  # noqa: BLE001 - settings optional; never block cache init
        return fallback


class MemoryCache:
    """Two-tier cache for memory system lookups.

    L1: In-process bounded LRU (shared :class:`BoundedLRU` primitive) —
        sub-millisecond, thread-safe lookups for hot data.
    L2: Shared :class:`CacheService` (Redis-like) — cross-worker, ~1ms lookups,
        guarded by a circuit breaker so a failing backend degrades to L1-only.
    """

    def __init__(
        self,
        *,
        l1_max_size: int = _DEFAULT_L1_SIZE,
        l2_service: "CacheService | None" = None,
        l2_ttl: int = _DEFAULT_L2_TTL,
        namespace: str = "memory",
    ) -> None:
        self._l1 = BoundedLRU(l1_max_size)
        self._l2 = l2_service
        self._l2_ttl = l2_ttl
        self._namespace = namespace
        self._hits = 0
        self._misses = 0
        # -- L2 circuit breaker state --
        self._l2_fail_count = 0
        self._l2_open_until = 0.0
        #: Central-registry stat name for this tier (e.g. ``memory:embeddings``).
        self._stat_name = f"memory:{namespace}"
        registry = get_cache_stats_registry()
        registry.register_size_provider(self._stat_name, lambda: self._l1.size)
        registry.register_detail_provider(self._stat_name, self._detail)

    def _detail(self) -> dict[str, Any]:
        """Pull-based diagnostics: L1 internals + live L2 circuit state."""
        return {
            "l1": self._l1.stats,
            "l2_enabled": self._l2 is not None,
            "l2_circuit_open": self._l2_circuit_open(),
            "l2_fail_count": self._l2_fail_count,
        }

    @property
    def hit_rate(self) -> float:
        total = self._hits + self._misses
        return self._hits / total if total > 0 else 0.0

    @staticmethod
    def cache_key(prefix: str, text: str, model: str = "") -> str:
        raw = f"{prefix}:{model}:{text}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]

    # -- L2 circuit breaker ----------------------------------------------

    def _l2_circuit_open(self) -> bool:
        return self._l2_open_until > 0.0 and time.monotonic() < self._l2_open_until

    def _l2_available(self) -> bool:
        return self._l2 is not None and not self._l2_circuit_open()

    def _l2_ok(self) -> None:
        """Record an L2 success, closing the breaker."""
        if self._l2_fail_count or self._l2_open_until:
            self._l2_fail_count = 0
            self._l2_open_until = 0.0

    def _l2_failed(self, exc: Exception) -> None:
        """Record an L2 failure, opening the breaker past the threshold."""
        self._l2_fail_count += 1
        if self._l2_fail_count >= _L2_FAIL_THRESHOLD and not self._l2_circuit_open():
            self._l2_open_until = time.monotonic() + _L2_COOLDOWN_SECONDS
            logger.warning(
                "memory_cache_l2_circuit_open namespace=%s fails=%d cooldown=%.0fs: %s",
                self._namespace,
                self._l2_fail_count,
                _L2_COOLDOWN_SECONDS,
                exc,
            )

    async def get(self, key: str) -> Any | None:
        value, hit = self._l1.get(key)
        if hit:
            self._hits += 1
            record_cache_event(self._stat_name, hit=True, entries=self._l1.size)
            return value

        if self._l2_available():
            try:
                val = await self._l2.get(  # type: ignore[union-attr]
                    f"{self._namespace}:{key}",
                    namespace=self._namespace,
                )
                self._l2_ok()
                if val is not None:
                    self._l1.set(key, val)
                    self._hits += 1
                    record_cache_event(self._stat_name, hit=True, entries=self._l1.size)
                    return val
            except Exception as exc:  # noqa: BLE001 - L2 is best-effort
                self._l2_failed(exc)

        self._misses += 1
        record_cache_event(self._stat_name, hit=False, entries=self._l1.size)
        return None

    async def set(self, key: str, value: Any, *, ttl: int | None = None) -> None:
        evicted = self._l1.set(key, value)
        record_cache_set(self._stat_name, evicted=evicted)

        if self._l2_available():
            try:
                await self._l2.set(  # type: ignore[union-attr]
                    f"{self._namespace}:{key}",
                    value,
                    namespace=self._namespace,
                    ttl=ttl or self._l2_ttl,
                )
                self._l2_ok()
            except Exception as exc:  # noqa: BLE001 - L2 is best-effort
                self._l2_failed(exc)
                logger.debug("memory_cache_l2_write_failed: %s", exc)

    async def invalidate(self, key: str) -> None:
        self._l1.delete(key)
        if self._l2_available():
            try:
                await self._l2.delete(  # type: ignore[union-attr]
                    f"{self._namespace}:{key}",
                    namespace=self._namespace,
                )
                self._l2_ok()
            except Exception as exc:  # noqa: BLE001 - L2 is best-effort
                self._l2_failed(exc)

    def clear_l1(self) -> None:
        self._l1.clear()


class EmbeddingCache(MemoryCache):
    """Specialized cache for embedding vectors.

    Keyed by ``sha256(text + model_name)`` so identical text embedded
    with the same model always cache-hits.
    """

    def __init__(
        self,
        *,
        l1_max_size: int | None = None,
        l2_service: "CacheService | None" = None,
        l2_ttl: int = 7200,
    ) -> None:
        super().__init__(
            l1_max_size=_resolve_l1_size(
                l1_max_size, "memory_embedding_l1_max_items", 1024
            ),
            l2_service=l2_service,
            l2_ttl=l2_ttl,
            namespace="embeddings",
        )

    async def get_embedding(
        self,
        text: str,
        model: str,
    ) -> list[float] | None:
        key = self.cache_key("emb", text, model)
        result = await self.get(key)
        if result is not None and isinstance(result, list):
            return result
        return None

    async def set_embedding(
        self,
        text: str,
        model: str,
        vector: list[float],
    ) -> None:
        key = self.cache_key("emb", text, model)
        await self.set(key, vector)


class RecallCache(MemoryCache):
    """Cache for recall query results.

    Short TTL since recall results are query-dependent, but saves
    redundant store lookups when the same query fires within a turn.
    """

    def __init__(
        self,
        *,
        l1_max_size: int | None = None,
        l2_service: "CacheService | None" = None,
    ) -> None:
        super().__init__(
            l1_max_size=_resolve_l1_size(
                l1_max_size, "memory_recall_l1_max_items", 256
            ),
            l2_service=l2_service,
            l2_ttl=300,
            namespace="recall",
        )
