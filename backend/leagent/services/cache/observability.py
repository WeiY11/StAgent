"""Central cache observability — one front door for hit/miss accounting.

Every cache layer in LeAgent (the :class:`CacheService` in-memory LRU, the
workflow node-output cache, the ``cache_manager`` tool cache, the LLM
tool-schema cache, and the tiered agent-memory cache) records its events
through :func:`record_cache_event`. That function:

1. Aggregates per-cache counters in an in-process :class:`CacheStatsRegistry`
   so the :pyfunc:`GET /api/v1/stats/cache` endpoint can return a real-time
   hit-rate snapshot without a Prometheus scrape, and
2. Forwards the same event to the Prometheus registry
   (``leagent_cache_hits_total`` / ``leagent_cache_misses_total`` /
   ``leagent_cache_entries``) so existing dashboards keep working.

Keeping the registry decoupled from ``prometheus_client`` means unit tests can
assert hit-rate maths deterministically via :meth:`CacheStatsRegistry.reset`.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

#: Sliding-window configuration for the "recent" hit rate. Cumulative rates
#: since process start mask regressions; a short window surfaces them. Kept
#: coarse (bucketed) so accounting stays O(1) per event.
_WINDOW_SECONDS = 300
_BUCKET_SECONDS = 15
_NUM_BUCKETS = _WINDOW_SECONDS // _BUCKET_SECONDS


@dataclass
class CacheStat:
    """Mutable per-cache counter bundle.

    ``size_provider`` is an optional callable returning the live entry count
    (e.g. ``lambda: len(self._cache)``) so the snapshot reflects the current
    resident set rather than a stale gauge. ``detail_provider`` returns extra
    cache-internal diagnostics (split evictions, backend, L2 state) that are
    pulled lazily at snapshot time rather than pushed on the hot path.
    """

    name: str
    hits: int = 0
    misses: int = 0
    sets: int = 0
    evictions: int = 0
    expirations: int = 0
    rejections: int = 0
    fills: int = 0
    coalesced: int = 0
    size_provider: Callable[[], int] | None = None
    detail_provider: Callable[[], dict[str, Any]] | None = None
    #: bucket_index -> [hits, misses] for the sliding window.
    _window: dict[int, list[int]] = field(default_factory=dict)

    @property
    def total_requests(self) -> int:
        return self.hits + self.misses

    @property
    def hit_rate(self) -> float:
        total = self.total_requests
        return self.hits / total if total > 0 else 0.0

    def _bucket(self, now: float) -> list[int]:
        idx = int(now // _BUCKET_SECONDS)
        # Prune buckets that fell out of the window.
        cutoff = idx - _NUM_BUCKETS
        for stale in [k for k in self._window if k <= cutoff]:
            del self._window[stale]
        return self._window.setdefault(idx, [0, 0])

    def record_window(self, now: float, *, hit: bool) -> None:
        bucket = self._bucket(now)
        bucket[0 if hit else 1] += 1

    def _recent(self, now: float) -> tuple[int, int]:
        cutoff = int(now // _BUCKET_SECONDS) - _NUM_BUCKETS
        h = sum(v[0] for k, v in self._window.items() if k > cutoff)
        m = sum(v[1] for k, v in self._window.items() if k > cutoff)
        return h, m

    def snapshot(self) -> dict[str, Any]:
        recent_h, recent_m = self._recent(time.time())
        recent_total = recent_h + recent_m
        out: dict[str, Any] = {
            "name": self.name,
            "hits": self.hits,
            "misses": self.misses,
            "sets": self.sets,
            "evictions": self.evictions,
            "expirations": self.expirations,
            "rejections": self.rejections,
            "fills": self.fills,
            "coalesced": self.coalesced,
            "total_requests": self.total_requests,
            "hit_rate": round(self.hit_rate, 4),
            "recent_hit_rate": round(recent_h / recent_total, 4) if recent_total else 0.0,
            "recent_requests": recent_total,
        }
        if self.size_provider is not None:
            try:
                out["size"] = int(self.size_provider())
            except Exception:  # noqa: BLE001 - size is best-effort
                out["size"] = None
        if self.detail_provider is not None:
            try:
                out["detail"] = dict(self.detail_provider())
            except Exception:  # noqa: BLE001 - detail is best-effort
                out["detail"] = None
        return out

    def copy_for_snapshot(self) -> CacheStat:
        """Return a lock-free copy for snapshotting outside the registry lock.

        Counters are copied by value and the sliding-window bucket lists are
        deep-copied so the returned stat never shares mutable state with the
        live one. The (idempotent) provider callables are copied by reference
        and invoked later, off the lock, so a slow ``size_provider`` /
        ``detail_provider`` can never block cache event recording.
        """
        clone = CacheStat(
            name=self.name,
            hits=self.hits,
            misses=self.misses,
            sets=self.sets,
            evictions=self.evictions,
            expirations=self.expirations,
            rejections=self.rejections,
            fills=self.fills,
            coalesced=self.coalesced,
            size_provider=self.size_provider,
            detail_provider=self.detail_provider,
        )
        clone._window = {k: list(v) for k, v in self._window.items()}
        return clone


class CacheStatsRegistry:
    """Thread-safe in-process aggregate of named cache statistics."""

    def __init__(self) -> None:
        self._stats: dict[str, CacheStat] = {}
        self._lock = threading.Lock()

    def _get(self, name: str) -> CacheStat:
        stat = self._stats.get(name)
        if stat is None:
            stat = CacheStat(name=name)
            self._stats[name] = stat
        return stat

    def record_hit(self, name: str, *, count: int = 1) -> None:
        now = time.time()
        with self._lock:
            stat = self._get(name)
            stat.hits += count
            for _ in range(count):
                stat.record_window(now, hit=True)

    def record_miss(self, name: str, *, count: int = 1) -> None:
        now = time.time()
        with self._lock:
            stat = self._get(name)
            stat.misses += count
            for _ in range(count):
                stat.record_window(now, hit=False)

    def record_set(self, name: str, *, count: int = 1) -> None:
        with self._lock:
            self._get(name).sets += count

    def record_eviction(self, name: str, *, count: int = 1) -> None:
        if count <= 0:
            return
        with self._lock:
            self._get(name).evictions += count

    def record_expiration(self, name: str, *, count: int = 1) -> None:
        if count <= 0:
            return
        with self._lock:
            self._get(name).expirations += count

    def record_rejection(self, name: str, *, count: int = 1) -> None:
        """Record *count* writes rejected (e.g. oversized value) for *name*."""
        if count <= 0:
            return
        with self._lock:
            self._get(name).rejections += count

    def record_fill(self, name: str, *, coalesced: int = 0) -> None:
        """Record a single-flight cache fill and its coalesced waiter count.

        ``coalesced`` is the number of concurrent callers that piggy-backed on
        this one factory execution instead of running their own — the direct
        measure of single-flight effectiveness.
        """
        with self._lock:
            stat = self._get(name)
            stat.fills += 1
            if coalesced > 0:
                stat.coalesced += coalesced

    def register_size_provider(self, name: str, provider: Callable[[], int]) -> None:
        """Attach a live entry-count provider for *name* (idempotent)."""
        with self._lock:
            self._get(name).size_provider = provider

    def register_detail_provider(
        self, name: str, provider: Callable[[], dict[str, Any]]
    ) -> None:
        """Attach a lazy diagnostics provider for *name* (idempotent)."""
        with self._lock:
            self._get(name).detail_provider = provider

    def snapshot(self) -> dict[str, Any]:
        """Return per-cache snapshots plus a rolled-up overall hit rate.

        Counters and provider callables are copied under the lock; the lock is
        then released before invoking ``size_provider`` / ``detail_provider``
        (via each copy's :meth:`CacheStat.snapshot`) so a slow provider cannot
        block the hot-path recorders or invert future locks.
        """
        with self._lock:
            rows = [stat.copy_for_snapshot() for stat in self._stats.values()]
        rows.sort(key=lambda item: item.name)
        caches = [row.snapshot() for row in rows]
        total_hits = sum(row.hits for row in rows)
        total_misses = sum(row.misses for row in rows)
        total = total_hits + total_misses
        return {
            "caches": caches,
            "total_hits": total_hits,
            "total_misses": total_misses,
            "total_requests": total,
            "overall_hit_rate": round(total_hits / total, 4) if total > 0 else 0.0,
        }

    def reset(self) -> None:
        with self._lock:
            self._stats.clear()


_registry: CacheStatsRegistry | None = None
_registry_lock = threading.Lock()


def get_cache_stats_registry() -> CacheStatsRegistry:
    """Return the process-wide :class:`CacheStatsRegistry` singleton."""
    global _registry
    if _registry is None:
        with _registry_lock:
            if _registry is None:
                _registry = CacheStatsRegistry()
    return _registry


def record_cache_event(
    cache_name: str,
    *,
    hit: bool,
    entries: int | None = None,
    evicted: int = 0,
    expired: int = 0,
) -> None:
    """Record a single cache access, mirroring it to Prometheus.

    Args:
        cache_name: Logical cache identifier, e.g. ``"cache:chat"``,
            ``"workflow_node_output"``, ``"tool_schema"``.
        hit: Whether the access was a hit.
        entries: Current resident entry count (updates the size gauge).
        evicted: Number of entries evicted for capacity (LRU) this operation.
        expired: Number of entries dropped for TTL expiry this operation.
    """
    reg = get_cache_stats_registry()
    if hit:
        reg.record_hit(cache_name)
    else:
        reg.record_miss(cache_name)
    if evicted:
        reg.record_eviction(cache_name, count=evicted)
    if expired:
        reg.record_expiration(cache_name, count=expired)

    _mirror_to_prometheus(cache_name, hit=hit, entries=entries)
    _mirror_pressure_to_prometheus(cache_name, evicted=evicted, expired=expired)


def record_cache_set(cache_name: str, *, evicted: int = 0, expired: int = 0) -> None:
    """Record a write (and any evictions/expirations it triggered)."""
    reg = get_cache_stats_registry()
    reg.record_set(cache_name)
    if evicted:
        reg.record_eviction(cache_name, count=evicted)
    if expired:
        reg.record_expiration(cache_name, count=expired)
    _mirror_pressure_to_prometheus(cache_name, evicted=evicted, expired=expired)


def record_cache_rejection(cache_name: str, *, reason: str = "oversized", count: int = 1) -> None:
    """Record a rejected write for *cache_name*.

    ``reason`` stays in-process only (never a Prometheus label) to keep metric
    cardinality bounded to the logical cache name.
    """
    get_cache_stats_registry().record_rejection(cache_name, count=count)
    try:
        from leagent.utils.metrics import get_metrics

        get_metrics().record_cache_rejection(cache_name)
    except Exception:  # noqa: BLE001
        pass


def record_cache_fill(
    cache_name: str, *, duration_sec: float = 0.0, coalesced: int = 0
) -> None:
    """Record a single-flight cache fill for *cache_name*.

    ``duration_sec`` (factory latency) and ``coalesced`` (waiters that shared
    the fill) mirror to Prometheus as low-cardinality series keyed only by
    ``cache_name``.
    """
    get_cache_stats_registry().record_fill(cache_name, coalesced=coalesced)
    try:
        from leagent.utils.metrics import get_metrics

        get_metrics().record_cache_fill(
            cache_name, duration_sec=duration_sec, coalesced=coalesced
        )
    except Exception:  # noqa: BLE001
        pass


def record_cache_expiration(cache_name: str, *, count: int = 1) -> None:
    """Record *count* TTL expirations swept for *cache_name*."""
    get_cache_stats_registry().record_expiration(cache_name, count=count)
    _mirror_pressure_to_prometheus(cache_name, evicted=0, expired=count)


def _mirror_to_prometheus(cache_name: str, *, hit: bool, entries: int | None) -> None:
    """Best-effort mirror to Prometheus; never let telemetry break a cache path."""
    try:
        from leagent.utils.metrics import get_metrics

        get_metrics().record_cache_event(cache_name, hit=hit, entries=entries)
    except Exception:  # noqa: BLE001
        pass


def _mirror_pressure_to_prometheus(
    cache_name: str, *, evicted: int = 0, expired: int = 0
) -> None:
    """Best-effort mirror of eviction/expiration counters to Prometheus."""
    if not evicted and not expired:
        return
    try:
        from leagent.utils.metrics import get_metrics

        metrics = get_metrics()
        if evicted:
            metrics.record_cache_eviction(cache_name, count=evicted)
        if expired:
            metrics.record_cache_expiration(cache_name, count=expired)
    except Exception:  # noqa: BLE001
        pass
