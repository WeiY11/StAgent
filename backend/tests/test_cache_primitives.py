"""Tests for the shared cache primitives and the second-wave optimizations.

Covers the pieces added on top of the original hit-rate work:

1. :class:`BoundedLRU` / :class:`ShardedLRU` — the single shared LRU primitive
   (O(1) LRU, TTL expiry, split ``lru_evictions`` vs ``expirations``, thread
   safety, ``delete_prefix`` / ``resize``).
2. Observability — sliding-window ``recent_hit_rate`` + pull-based
   ``detail_provider``.
3. ``CacheService`` — the fixed ``increment`` (per-namespace TTL + observed),
   batch ``delete_prefix`` / ``clear_namespace``, and runtime ``update_limits``.
4. ``MemoryCache`` — L1 on the primitive + the L2 circuit breaker that degrades
   to L1-only when the shared backend flaps.
"""

from __future__ import annotations

import threading

import pytest

from leagent.config.settings import Settings
from leagent.memory.cache import (
    _L2_FAIL_THRESHOLD,
    MemoryCache,
)
from leagent.services.cache.lru import BoundedLRU, ShardedLRU, make_lru
from leagent.services.cache.observability import (
    CacheStatsRegistry,
    get_cache_stats_registry,
)
from leagent.services.cache.service import CacheService


@pytest.fixture(autouse=True)
def _reset_registry():
    get_cache_stats_registry().reset()
    yield
    get_cache_stats_registry().reset()


def _make_service() -> CacheService:
    return CacheService(Settings())


# ---------------------------------------------------------------------------
# 1. BoundedLRU primitive
# ---------------------------------------------------------------------------


def test_bounded_lru_hit_miss_and_recency_eviction():
    lru = BoundedLRU(max_size=2)
    assert lru.set("a", 1) == 0
    assert lru.set("b", 2) == 0
    assert lru.get("a") == (1, True)  # "a" now MRU
    evicted = lru.set("c", 3)  # over capacity -> evict LRU ("b")
    assert evicted == 1
    assert lru.get("b") == (None, False)
    assert lru.get("a") == (1, True)
    assert lru.get("c") == (3, True)


def test_bounded_lru_split_lru_evictions_vs_expirations():
    lru = BoundedLRU(max_size=1)
    lru.set("a", 1)
    lru.set("b", 2)  # capacity eviction of "a"
    lru.set("c", 3, ttl=-1)  # inserted already-expired (evicts "b" for capacity)
    lru.get("c")  # expired -> counts as expiration + miss
    stats = lru.stats
    assert stats["lru_evictions"] == 2  # "a" then "b"
    assert stats["expirations"] == 1  # "c" via TTL
    assert stats["misses"] == 1


def test_bounded_lru_falsy_values_hit():
    lru = BoundedLRU(max_size=4)
    for i, val in enumerate([0, "", [], False]):
        lru.set(f"k{i}", val)
    for i, val in enumerate([0, "", [], False]):
        assert lru.get(f"k{i}") == (val, True)


def test_bounded_lru_peek_does_not_change_recency_or_counters():
    lru = BoundedLRU(max_size=2)
    lru.set("a", 1)
    lru.set("b", 2)
    assert lru.peek("a") == (1, True)
    # peek must not have promoted "a"; inserting "c" evicts the true LRU "a".
    lru.set("c", 3)
    assert lru.peek("a") == (None, False)
    assert lru.stats["hits"] == 0  # peek never touched hit/miss


def test_bounded_lru_delete_prefix_single_pass():
    lru = BoundedLRU(max_size=10)
    lru.set("app:chat:1", 1)
    lru.set("app:chat:2", 2)
    lru.set("app:files:1", 3)
    removed = lru.delete_prefix("app:chat:")
    assert removed == 2
    assert lru.get("app:files:1") == (3, True)
    assert lru.size == 1


def test_bounded_lru_resize_shrinks_and_evicts():
    lru = BoundedLRU(max_size=5)
    for i in range(5):
        lru.set(str(i), i)
    evicted = lru.resize(2)
    assert evicted == 3
    assert lru.size == 2
    assert lru.max_size == 2


def test_bounded_lru_is_thread_safe_under_contention():
    lru = BoundedLRU(max_size=1000)

    def worker(base: int) -> None:
        for i in range(1000):
            lru.set(f"{base}:{i}", i)
            lru.get(f"{base}:{i}")

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # No lost updates / corruption: size respects the bound and counters are sane.
    assert lru.size <= 1000
    stats = lru.stats
    assert stats["sets"] == 8000
    assert stats["hits"] + stats["misses"] == 8000


# ---------------------------------------------------------------------------
# 2. ShardedLRU + factory
# ---------------------------------------------------------------------------


def test_make_lru_returns_bounded_for_single_shard():
    assert isinstance(make_lru(10, shards=1), BoundedLRU)
    assert isinstance(make_lru(10, shards=4), ShardedLRU)


def test_sharded_lru_roundtrips_and_aggregates_stats():
    lru = ShardedLRU(max_size=64, shards=4)
    for i in range(50):
        lru.set(f"k{i}", i)
    hits = sum(1 for i in range(50) if lru.get(f"k{i}")[1])
    stats = lru.stats
    assert stats["shards"] == 4
    assert stats["sets"] == 50
    assert stats["hits"] == hits
    # Aggregate size never exceeds the total bound.
    assert stats["size"] <= stats["max_size"]


def test_sharded_lru_delete_prefix_spans_shards():
    lru = ShardedLRU(max_size=64, shards=4)
    for i in range(20):
        lru.set(f"pre:{i}", i)
    lru.set("other", 1)
    removed = lru.delete_prefix("pre:")
    assert removed == 20
    assert lru.get("other") == (1, True)


# ---------------------------------------------------------------------------
# 3. Observability — sliding window + detail provider
# ---------------------------------------------------------------------------


def test_recent_hit_rate_tracks_window():
    reg = CacheStatsRegistry()
    for _ in range(3):
        reg.record_hit("c")
    reg.record_miss("c")
    snap = reg.snapshot()
    c = next(x for x in snap["caches"] if x["name"] == "c")
    assert c["recent_requests"] == 4
    assert c["recent_hit_rate"] == 0.75


def test_detail_provider_is_pulled_lazily():
    reg = CacheStatsRegistry()
    calls = {"n": 0}

    def detail():
        calls["n"] += 1
        return {"backend": "memory", "flag": True}

    reg.register_detail_provider("c", detail)
    reg.record_hit("c")
    assert calls["n"] == 0  # not evaluated on the hot path
    snap = reg.snapshot()
    c = next(x for x in snap["caches"] if x["name"] == "c")
    assert c["detail"] == {"backend": "memory", "flag": True}
    assert calls["n"] == 1


def test_expiration_counter_is_recorded():
    reg = CacheStatsRegistry()
    reg.record_expiration("c", count=3)
    snap = reg.snapshot()
    c = next(x for x in snap["caches"] if x["name"] == "c")
    assert c["expirations"] == 3


# ---------------------------------------------------------------------------
# 4. CacheService — increment fix, batch delete, runtime limits
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_increment_is_observed_and_uses_namespace_ttl():
    svc = _make_service()
    assert await svc.increment("counter", namespace="chat") == 1
    assert await svc.increment("counter", amount=4, namespace="chat") == 5
    # The read side of increment is instrumented (hit on the 2nd call).
    stats = svc.memory_stats()
    assert stats["hits"] >= 1
    # And the stored value is a real hit afterwards.
    assert await svc.get("counter", namespace="chat") == 5


@pytest.mark.asyncio
async def test_delete_prefix_and_clear_namespace_are_batched():
    svc = _make_service()
    await svc.set("a", 1, namespace="chat")
    await svc.set("b", 2, namespace="chat")
    await svc.set("keep", 9, namespace="files")

    removed = await svc.delete_prefix("", namespace="chat")
    assert removed == 2
    assert await svc.get("keep", namespace="files") == 9

    await svc.set("x", 1, namespace="chat")
    cleared = await svc.clear_namespace("chat")
    assert cleared == 1


@pytest.mark.asyncio
async def test_update_limits_resizes_at_runtime():
    svc = _make_service()
    for i in range(20):
        await svc.set(f"k{i}", i, namespace="chat")
    cfg = svc.update_limits(max_items=5)
    assert cfg["max_items"] == 5
    assert cfg["resident_items"] <= 5
    assert svc.memory_stats()["max_size"] == 5


def test_config_snapshot_exposes_live_config():
    svc = _make_service()
    cfg = svc.config_snapshot()
    assert cfg["max_items"] == Settings().cache.max_items
    assert cfg["shard_count"] == Settings().cache.shard_count
    assert "namespace_ttls" in cfg


@pytest.mark.asyncio
async def test_cache_service_can_shard():
    settings = Settings()
    settings.cache.shard_count = 4
    settings.cache.max_items = 64
    svc = CacheService(settings)
    for i in range(30):
        await svc.set(f"k{i}", i, namespace="chat")
    assert await svc.get("k0", namespace="chat") == 0


# ---------------------------------------------------------------------------
# 5. MemoryCache — L1 primitive + L2 circuit breaker
# ---------------------------------------------------------------------------


class _FlakyL2:
    """Minimal CacheService-shaped stub that always fails."""

    def __init__(self) -> None:
        self.get_calls = 0
        self.set_calls = 0

    async def get(self, key, *, namespace=None):
        self.get_calls += 1
        raise ConnectionError("l2 down")

    async def set(self, key, value, *, namespace=None, ttl=None):
        self.set_calls += 1
        raise ConnectionError("l2 down")

    async def delete(self, key, *, namespace=None):
        raise ConnectionError("l2 down")


@pytest.mark.asyncio
async def test_memory_cache_l1_hit_after_set():
    mc = MemoryCache(l1_max_size=8)
    await mc.set("k", [1, 2, 3])
    assert await mc.get("k") == [1, 2, 3]
    assert mc.hit_rate == 1.0


@pytest.mark.asyncio
async def test_memory_cache_l1_is_bounded():
    mc = MemoryCache(l1_max_size=2)
    await mc.set("a", 1)
    await mc.set("b", 2)
    await mc.set("c", 3)  # evicts "a"
    assert await mc.get("a") is None
    assert await mc.get("c") == 3


@pytest.mark.asyncio
async def test_l2_circuit_breaker_opens_and_stops_calling_l2():
    l2 = _FlakyL2()
    mc = MemoryCache(l1_max_size=8, l2_service=l2)

    # Each miss goes to L2 and fails; after the threshold the breaker opens.
    for i in range(_L2_FAIL_THRESHOLD):
        await mc.get(f"miss{i}")

    assert mc._l2_circuit_open() is True
    calls_at_open = l2.get_calls

    # Further misses must NOT reach the (now open) L2 — degraded to L1-only.
    await mc.get("another")
    assert l2.get_calls == calls_at_open

    detail = mc._detail()
    assert detail["l2_circuit_open"] is True
    assert detail["l2_enabled"] is True


@pytest.mark.asyncio
async def test_l2_write_failure_degrades_without_raising():
    l2 = _FlakyL2()
    mc = MemoryCache(l1_max_size=8, l2_service=l2)
    # A failing L2 write must not surface to the caller; L1 still holds it.
    await mc.set("k", 123)
    assert await mc.get("k") == 123


# ---------------------------------------------------------------------------
# 6. Task 2 — exact capacity, monotonic expiry, atomic updates
# ---------------------------------------------------------------------------


def test_sharded_capacity_equals_requested_capacity() -> None:
    lru = ShardedLRU(max_size=10, shards=3)
    assert lru.max_size == 10


def test_atomic_update_does_not_lose_concurrent_increments() -> None:
    lru = BoundedLRU(max_size=4)

    def worker() -> None:
        for _ in range(1_000):
            lru.update("counter", lambda value: int(value) + 1, default=0)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert lru.get("counter") == (8_000, True)


def test_zero_ttl_is_immediately_expired() -> None:
    lru = BoundedLRU(max_size=2)
    lru.set("k", "v", ttl=0)
    assert lru.get("k") == (None, False)
