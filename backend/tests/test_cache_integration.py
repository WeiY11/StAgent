"""Integration tests for memory caching wired through the shared CacheService.

Covers two guarantees Task 7 introduces:

1. Identical concurrent embedding requests collapse to a single provider call.
2. Recall cache keys are isolated by owner, so two users never share a bundle.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from uuid import UUID

import pytest

from leagent.config.settings import Settings
from leagent.memory.embeddings import LLMServiceEmbeddingProvider, NullEmbeddingProvider
from leagent.memory.recall import RecallOptions, RetrievalPipeline
from leagent.services.cache.observability import get_cache_stats_registry
from leagent.services.cache.service import CacheService


class FakeLLM:
    def __init__(self) -> None:
        self.embed_calls = 0

    async def embed(self, texts, *, model=None):  # noqa: ANN001
        self.embed_calls += 1
        await asyncio.sleep(0.01)
        return SimpleNamespace(embeddings=[[1.0, 0.0, 0.0] for _ in texts])


class DisabledCollection:
    can_search = False


class CountingStore:
    def __init__(self) -> None:
        self.collection = DisabledCollection()
        self.search_calls = 0

    async def lexical_search(self, query, **kwargs):  # noqa: ANN001
        self.search_calls += 1
        return []


@pytest.mark.asyncio
async def test_embedding_provider_reuses_identical_model_text() -> None:
    cache_service = CacheService(Settings())
    fake_llm = FakeLLM()
    provider = LLMServiceEmbeddingProvider(
        fake_llm, model="embed-v1", dimension=3, cache=cache_service
    )
    first, second = await asyncio.gather(
        provider.embed_one("same"), provider.embed_one("same")
    )
    assert first == second
    assert fake_llm.embed_calls == 1


@pytest.mark.asyncio
async def test_recall_key_isolated_by_user() -> None:
    cache_service = CacheService(Settings())
    episodic = CountingStore()
    semantic = CountingStore()
    procedural = CountingStore()
    pipeline = RetrievalPipeline(
        episodic=episodic,
        semantic=semantic,
        procedural=procedural,
        embeddings=NullEmbeddingProvider(dimension=3),
        cache=cache_service,
    )
    await pipeline.recall(RecallOptions(query="q", user_id=UUID(int=1)))
    await pipeline.recall(RecallOptions(query="q", user_id=UUID(int=2)))
    assert episodic.search_calls == 2


def _rejections_for(stat_name: str) -> int:
    """Read the cumulative rejection counter for *stat_name* from the singleton."""
    for cache in get_cache_stats_registry().snapshot()["caches"]:
        if cache["name"] == stat_name:
            return int(cache.get("rejections", 0))
    return 0


@pytest.mark.asyncio
async def test_oversized_value_is_rejected_without_caching() -> None:
    # Task 4 invariant: an oversized single value must not be cached (it would
    # evict the whole working set), the write reports failure, the value stays
    # absent, and the rejection counter increments.
    service = CacheService(Settings())
    before = _rejections_for("cache:chat")
    oversized = "x" * (service._cache_cfg.max_value_bytes + 1_024)

    stored = await service.set("huge", oversized, namespace="chat")

    assert stored is False
    assert await service.get("huge", namespace="chat", default="MISS") == "MISS"
    assert _rejections_for("cache:chat") == before + 1


@pytest.mark.asyncio
async def test_resident_set_stays_bounded_under_many_unique_writes() -> None:
    # Task 4 invariant: 20,000 unique writes against a 1,000-item cap must never
    # let the resident set exceed the cap (bounded memory, LRU eviction).
    settings = Settings()
    settings.cache.max_items = 1_000
    service = CacheService(settings)
    for i in range(20_000):
        await service.set(f"k{i}", i, namespace="chat")
    assert service.memory_stats()["size"] <= 1_000
