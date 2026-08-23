"""Source-level memoisation with scope-aware TTL."""

from __future__ import annotations

import time
from dataclasses import dataclass

from StAgent.context.types import ContextBlock, ContextScope
from StAgent.services.cache.lru import BoundedLRU
from StAgent.services.cache.observability import (
    record_cache_event,
    record_cache_set,
)

__all__ = [
    "CacheEntry",
    "SourceCache",
]


@dataclass(slots=True)
class CacheEntry:
    block: ContextBlock
    created_at: float
    hit_count: int = 0


class SourceCache:
    """Per-source memo keyed by invalidation_key with scope-aware TTL.

    Backed by the shared thread-safe :class:`BoundedLRU` so the context cache
    is bounded (no unbounded growth across long-lived processes) and reports
    hits / misses / evictions under the ``context_source`` metric name. Keys are
    namespaced by scope so ``process`` and ``session`` entries never collide, and
    ``turn``-scoped resolutions are never inserted (they are valid for a single
    turn only).
    """

    def __init__(
        self,
        *,
        max_size: int = 256,
        process_ttl_sec: float = 300.0,
        session_ttl_sec: float = 900.0,
    ) -> None:
        self._lru = BoundedLRU(max_size)
        self._ttls = {
            ContextScope.PROCESS: process_ttl_sec,
            ContextScope.SESSION: session_ttl_sec,
        }

    @staticmethod
    def _key(key: str, scope: ContextScope) -> str:
        return f"{scope.value}:{key}"

    def get(self, key: str, scope: ContextScope) -> ContextBlock | None:
        if scope is ContextScope.TURN:
            record_cache_event("context_source", hit=False, entries=self._lru.size)
            return None
        entry, hit = self._lru.get(self._key(key, scope))
        record_cache_event("context_source", hit=hit, entries=self._lru.size)
        if not hit:
            return None
        entry.hit_count += 1
        return entry.block

    def put(self, key: str, block: ContextBlock, scope: ContextScope) -> None:
        if scope is ContextScope.TURN:
            return
        evicted = self._lru.set(
            self._key(key, scope),
            CacheEntry(block=block, created_at=time.monotonic()),
            ttl=self._ttls[scope],
        )
        record_cache_set("context_source", evicted=evicted)

    def clear(self) -> None:
        self._lru.clear()

    @property
    def stats(self) -> dict[str, int]:
        st = self._lru.stats
        return {"hits": st["hits"], "misses": st["misses"], "size": st["size"]}
