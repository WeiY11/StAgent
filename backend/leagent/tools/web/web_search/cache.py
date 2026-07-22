"""Simple in-process TTL cache for web search / fetch results."""

from __future__ import annotations

from typing import Any, Generic, TypeVar

from leagent.services.cache.lru import BoundedLRU
from leagent.services.cache.observability import (
    record_cache_event,
    record_cache_set,
)

T = TypeVar("T")


class TtlCache(Generic[T]):
    """Thread-safe, bounded per-entry TTL cache over the shared LRU primitive.

    Backed by :class:`BoundedLRU` so capacity and TTL eviction are precise and
    observable. Each instance reports under its own ``metric_name`` so the
    ``web_search`` and ``web_fetch`` caches stay independently actionable.
    """

    def __init__(
        self,
        *,
        default_ttl_sec: float = 900.0,
        max_entries: int = 256,
        metric_name: str = "web_tool",
    ) -> None:
        self._default_ttl = max(0.0, float(default_ttl_sec))
        self._metric_name = metric_name
        self._lru: BoundedLRU = BoundedLRU(max(1, int(max_entries)))

    def get(self, key: str) -> T | None:
        value, hit = self._lru.get(key)
        record_cache_event(self._metric_name, hit=hit, entries=self._lru.size)
        return value if hit else None

    def set(self, key: str, value: T, *, ttl_sec: float | None = None) -> None:
        ttl = self._default_ttl if ttl_sec is None else float(ttl_sec)
        evicted = self._lru.set(key, value, ttl=ttl)
        record_cache_set(self._metric_name, evicted=evicted)

    def clear(self) -> None:
        self._lru.clear()


# Module-level caches shared across tool calls in-process.
_search_cache: TtlCache[dict[str, Any]] | None = None
_fetch_cache: TtlCache[dict[str, Any]] | None = None


def get_search_cache(*, ttl_minutes: float = 15.0) -> TtlCache[dict[str, Any]]:
    global _search_cache
    if _search_cache is None:
        _search_cache = TtlCache(
            default_ttl_sec=max(0.0, ttl_minutes) * 60.0,
            metric_name="web_search",
        )
    return _search_cache


def get_fetch_cache(*, ttl_minutes: float = 15.0) -> TtlCache[dict[str, Any]]:
    global _fetch_cache
    if _fetch_cache is None:
        _fetch_cache = TtlCache(
            default_ttl_sec=max(0.0, ttl_minutes) * 60.0,
            metric_name="web_fetch",
        )
    return _fetch_cache


def reset_web_caches() -> None:
    """Test / settings-reload helper."""
    global _search_cache, _fetch_cache
    if _search_cache is not None:
        _search_cache.clear()
    if _fetch_cache is not None:
        _fetch_cache.clear()
    _search_cache = None
    _fetch_cache = None
