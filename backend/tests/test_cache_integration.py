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
