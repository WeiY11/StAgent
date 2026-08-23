"""Contracts for capability-aware knowledge embedding startup."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from StAgent.knowledge.embedding_runtime import resolve_knowledge_embedding_runtime
from StAgent.llm.base import EmbeddingResponse
from StAgent.memory.embeddings import LLMServiceEmbeddingProvider


class _Registry:
    def __init__(self, providers: dict[str, object]) -> None:
        self._providers = providers

    def has_provider(self, name: str) -> bool:
        return name in self._providers

    def get_provider(self, name: str) -> object:
        return self._providers[name]


class _Service:
    def __init__(self, providers: dict[str, object]) -> None:
        self.registry = _Registry(providers)


def test_deepseek_route_is_reported_unavailable_for_embeddings() -> None:
    runtime = resolve_knowledge_embedding_runtime(
        _Service({"deepseek": SimpleNamespace(supports_embeddings=False)}),
        cache=None,
        enabled=True,
        provider="deepseek",
        model="deepseek-v4-flash",
        dimension=1024,
    )

    assert runtime.provider is None
    assert runtime.status.reason == "provider_lacks_embeddings"
    assert runtime.status.to_dict() == {
        "enabled": True,
        "provider": "deepseek",
        "model": "deepseek-v4-flash",
        "dimension": 1024,
        "reason": "provider_lacks_embeddings",
    }


def test_embedding_capable_provider_is_bound_explicitly() -> None:
    runtime = resolve_knowledge_embedding_runtime(
        _Service({"ollama": SimpleNamespace(supports_embeddings=True)}),
        cache=None,
        enabled=True,
        provider="ollama",
        model="nomic-embed-text",
        dimension=768,
    )

    assert isinstance(runtime.provider, LLMServiceEmbeddingProvider)
    assert runtime.provider.provider_name == "ollama"
    assert runtime.provider.model == "ollama:nomic-embed-text"
    assert runtime.status.reason == "ready"


def test_unconfigured_embedding_route_never_falls_back_to_chat_provider() -> None:
    runtime = resolve_knowledge_embedding_runtime(
        _Service({"deepseek": SimpleNamespace(supports_embeddings=False)}),
        cache=None,
        enabled=True,
        provider="",
        model="",
        dimension=1024,
    )

    assert runtime.provider is None
    assert runtime.status.reason == "embedding_route_unconfigured"


@pytest.mark.asyncio
async def test_embedding_provider_sends_explicit_provider_and_model() -> None:
    calls: list[dict[str, object]] = []

    class _EmbeddingService:
        async def embed(self, texts: list[str], **kwargs: object) -> EmbeddingResponse:
            calls.append({"texts": texts, **kwargs})
            return EmbeddingResponse(embeddings=[[0.1, 0.2]])

    provider = LLMServiceEmbeddingProvider(
        _EmbeddingService(),  # type: ignore[arg-type]
        provider="ollama",
        model="nomic-embed-text",
        dimension=2,
    )

    assert await provider.embed_one("semantic retrieval") == [0.1, 0.2]
    assert calls == [
        {
            "texts": ["semantic retrieval"],
            "provider": "ollama",
            "model": "nomic-embed-text",
        }
    ]
