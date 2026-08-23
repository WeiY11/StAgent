"""Cache hit-rate optimization tests.

Covers the four load-bearing changes made to raise (and observe) the aggregate
cache hit rate:

1. Central :class:`CacheStatsRegistry` — deterministic hit-rate maths + rollup.
2. ``CacheService`` falsy-value fix (sentinel) — a cached ``0`` / ``""`` / ``[]``
   now *hits* instead of missing on every read.
3. Per-namespace TTL + ``warm()`` — the primary lever for a higher hit rate.
4. ``cache_manager`` ``LocalCache`` — now bounded (LRU) and instrumented.

The final test demonstrates a measurable hit-rate improvement from warming.
"""

from __future__ import annotations

import pytest

from StAgent.config.settings import Settings
from StAgent.services.cache.observability import (
    CacheStatsRegistry,
    get_cache_stats_registry,
    record_cache_event,
    record_cache_set,
)
from StAgent.services.cache.service import CacheService
from StAgent.tools.util.cache_manager import LocalCache


@pytest.fixture(autouse=True)
def _reset_registry():
    """Isolate each test from cross-test registry accumulation."""
    get_cache_stats_registry().reset()
    yield
    get_cache_stats_registry().reset()


def _make_service() -> CacheService:
    return CacheService(Settings())


# ---------------------------------------------------------------------------
# 1. Central registry — hit-rate maths + rollup
# ---------------------------------------------------------------------------


def test_registry_hit_rate_math_is_deterministic():
    reg = CacheStatsRegistry()
    for _ in range(3):
        reg.record_hit("cache:chat")
    reg.record_miss("cache:chat")

    snap = reg.snapshot()
    chat = next(c for c in snap["caches"] if c["name"] == "cache:chat")
    assert chat["hits"] == 3
    assert chat["misses"] == 1
    assert chat["total_requests"] == 4
    assert chat["hit_rate"] == 0.75


def test_registry_overall_rollup_across_caches():
    reg = CacheStatsRegistry()
    reg.record_hit("a")
    reg.record_hit("a")
    reg.record_miss("b")
    reg.record_hit("b")

    snap = reg.snapshot()
    assert snap["total_hits"] == 3
    assert snap["total_misses"] == 1
    assert snap["total_requests"] == 4
    assert snap["overall_hit_rate"] == 0.75
    # caches sorted by name for stable output
    assert [c["name"] for c in snap["caches"]] == ["a", "b"]


def test_registry_size_provider_reflects_live_count():
    reg = CacheStatsRegistry()
    backing: dict[str, int] = {}
    reg.register_size_provider("live", lambda: len(backing))
    backing["x"] = 1
    backing["y"] = 2
    snap = reg.snapshot()
    live = next(c for c in snap["caches"] if c["name"] == "live")
    assert live["size"] == 2


def test_record_cache_event_flows_to_singleton():
    record_cache_event("tool_schema", hit=True)
    record_cache_event("tool_schema", hit=False)
    record_cache_set("tool_schema", evicted=2)

    snap = get_cache_stats_registry().snapshot()
    ts = next(c for c in snap["caches"] if c["name"] == "tool_schema")
    assert ts["hits"] == 1
    assert ts["misses"] == 1
    assert ts["sets"] == 1
    assert ts["evictions"] == 2


def test_cache_snapshot_exposes_pressure_and_fill_metrics() -> None:
    reg = CacheStatsRegistry()
    reg.record_rejection("chat")
    reg.record_fill("chat", coalesced=9)
    cache = reg.snapshot()["caches"][0]
    assert cache["rejections"] == 1
    assert cache["fills"] == 1
    assert cache["coalesced"] == 9


# ---------------------------------------------------------------------------
# 2. CacheService falsy-value fix (the core bug)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("falsy", [0, "", [], False, {}])
async def test_cached_falsy_value_is_a_hit_not_a_miss(falsy):
    svc = _make_service()
    await svc.set("k", falsy, namespace="chat")

    # Reading a cached falsy value must return it (and register a hit).
    got = await svc.get("k", namespace="chat", default="SENTINEL_DEFAULT")
    assert got == falsy
    assert got != "SENTINEL_DEFAULT"

    stats = svc.memory_stats()
    assert stats["hits"] == 1
    assert stats["misses"] == 0


@pytest.mark.asyncio
async def test_get_or_set_does_not_rerun_factory_for_cached_falsy():
    svc = _make_service()
    calls = {"n": 0}

    async def factory():
        calls["n"] += 1
        return 0  # falsy result

    first = await svc.get_or_set("z", factory, namespace="chat")
    second = await svc.get_or_set("z", factory, namespace="chat")
    assert first == 0
    assert second == 0
    # Factory must run exactly once — the cached 0 is a hit on the 2nd call.
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_missing_key_returns_default_and_records_miss():
    svc = _make_service()
    got = await svc.get("absent", namespace="chat", default="fallback")
    assert got == "fallback"
    assert svc.memory_stats()["misses"] == 1


# ---------------------------------------------------------------------------
# 3. Per-namespace TTL + warm()
# ---------------------------------------------------------------------------


def test_namespace_ttls_differ_from_default():
    cfg = Settings().cache
    assert cfg.ttl_for("recall") == 300
    assert cfg.ttl_for("embeddings") == 7200
    assert cfg.ttl_for("chat") == 900
    assert cfg.ttl_for("workflow_node_output") == 900
    # Unknown namespace falls back to the default TTL.
    assert cfg.ttl_for("unknown") == cfg.default_ttl
    assert cfg.ttl_for(None) == cfg.default_ttl


@pytest.mark.asyncio
async def test_warm_preloads_keys_and_they_hit():
    svc = _make_service()
    n = await svc.warm({"a": 1, "b": 2, "c": 3}, namespace="embeddings")
    assert n == 3

    assert await svc.get("a", namespace="embeddings") == 1
    assert await svc.get("b", namespace="embeddings") == 2
    assert await svc.get("c", namespace="embeddings") == 3
    assert svc.memory_stats()["hits"] == 3


# ---------------------------------------------------------------------------
# 4. cache_manager LocalCache — bounded LRU + instrumentation
# ---------------------------------------------------------------------------


def test_local_cache_evicts_lru_past_max_size():
    cache = LocalCache(max_size=3)
    cache.set("a", 1)
    cache.set("b", 2)
    cache.set("c", 3)
    # Touch "a" so it is most-recently-used; "b" becomes the LRU victim.
    cache.get("a")
    cache.set("d", 4)  # exceeds max_size -> evict LRU ("b")

    assert cache.size == 3
    assert cache.get("b")[1] is False  # evicted
    assert cache.get("a")[1] is True
    assert cache.get("c")[1] is True
    assert cache.get("d")[1] is True
    assert cache.get_stats()["evictions"] >= 1


def test_local_cache_reports_to_registry():
    cache = LocalCache(max_size=10)
    cache.set("k", "v")
    cache.get("k")       # hit
    cache.get("absent")  # miss

    snap = get_cache_stats_registry().snapshot()
    tool = next(c for c in snap["caches"] if c["name"] == "cache_manager_tool")
    assert tool["hits"] == 1
    assert tool["misses"] == 1
    assert tool["hit_rate"] == 0.5


def test_local_cache_expired_entry_is_a_miss():
    cache = LocalCache(max_size=10)
    cache.set("k", "v", ttl=-1)  # already expired
    value, found = cache.get("k")
    assert found is False
    assert value is None


# ---------------------------------------------------------------------------
# 5. End-to-end: warming raises the measured hit rate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_warming_improves_hit_rate_vs_cold_cache():
    keys = [f"emb:{i}" for i in range(10)]

    # Cold cache: first read of each key misses.
    cold = _make_service()
    for k in keys:
        await cold.get(k, namespace="embeddings")
    cold_rate = cold.memory_stats()["hit_rate"]
    assert cold_rate == 0.0  # every access missed

    # Warmed cache: same read pattern now hits.
    warm = _make_service()
    await warm.warm({k: k.encode() for k in keys}, namespace="embeddings")
    for k in keys:
        await warm.get(k, namespace="embeddings")
    warm_rate = warm.memory_stats()["hit_rate"]

    assert warm_rate == 1.0
    assert warm_rate > cold_rate
