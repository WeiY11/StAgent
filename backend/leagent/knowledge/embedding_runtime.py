"""Capability-aware embedding wiring for the knowledge retrieval plane.

The knowledge pipeline must never treat a chat-only provider as an embedding
backend.  This module performs a pure, local capability check during service
bootstrap; it deliberately makes no provider or vector-store request.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from leagent.memory.embeddings import LLMServiceEmbeddingProvider

if TYPE_CHECKING:
    from leagent.llm.service import LLMService
    from leagent.memory.embeddings import EmbeddingProvider
    from leagent.services.cache.service import CacheService


@dataclass(frozen=True)
class KnowledgeEmbeddingStatus:
    """Secret-free outcome of resolving the optional embedding route."""

    enabled: bool
    provider: str | None
    model: str | None
    dimension: int
    reason: str

    def to_dict(self) -> dict[str, bool | int | str | None]:
        """Return values suitable for structured logs and health responses."""
        return {
            "enabled": self.enabled,
            "provider": self.provider,
            "model": self.model,
            "dimension": self.dimension,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class KnowledgeEmbeddingRuntime:
    """Resolved embedding implementation plus its safe diagnostic state."""

    provider: EmbeddingProvider | None
    status: KnowledgeEmbeddingStatus


def _normalized_dimension(value: int) -> int:
    """Keep direct factory callers safe when they bypass Pydantic settings."""
    try:
        return max(8, min(int(value), 16384))
    except (TypeError, ValueError):
        return 1024


def _runtime(
    *,
    enabled: bool,
    provider: str | None,
    model: str | None,
    dimension: int,
    reason: str,
    embedding_provider: EmbeddingProvider | None = None,
) -> KnowledgeEmbeddingRuntime:
    return KnowledgeEmbeddingRuntime(
        provider=embedding_provider,
        status=KnowledgeEmbeddingStatus(
            enabled=enabled,
            provider=provider,
            model=model,
            dimension=dimension,
            reason=reason,
        ),
    )


def resolve_knowledge_embedding_runtime(
    llm_service: LLMService | None,
    *,
    cache: CacheService | None,
    enabled: bool,
    provider: str = "",
    model: str = "",
    dimension: int = 1024,
) -> KnowledgeEmbeddingRuntime:
    """Resolve an embedding provider without performing a remote request.

    Both ``provider`` and ``model`` are required when semantic retrieval is
    enabled.  Requiring an explicit route prevents the embedding service from
    falling back to the chat route, which would make a DeepSeek-only deployment
    appear vector-capable until its first indexing request fails.
    """
    resolved_provider = provider.strip() or None
    resolved_model = model.strip() or None
    resolved_dimension = _normalized_dimension(dimension)

    if not enabled:
        return _runtime(
            enabled=False,
            provider=resolved_provider,
            model=resolved_model,
            dimension=resolved_dimension,
            reason="disabled",
        )
    if llm_service is None:
        return _runtime(
            enabled=True,
            provider=resolved_provider,
            model=resolved_model,
            dimension=resolved_dimension,
            reason="llm_service_unavailable",
        )
    if not resolved_provider or not resolved_model:
        return _runtime(
            enabled=True,
            provider=resolved_provider,
            model=resolved_model,
            dimension=resolved_dimension,
            reason="embedding_route_unconfigured",
        )

    registry = getattr(llm_service, "registry", None)
    has_provider = getattr(registry, "has_provider", None)
    get_provider = getattr(registry, "get_provider", None)
    if not callable(has_provider) or not callable(get_provider):
        return _runtime(
            enabled=True,
            provider=resolved_provider,
            model=resolved_model,
            dimension=resolved_dimension,
            reason="provider_registry_unavailable",
        )
    if not has_provider(resolved_provider):
        return _runtime(
            enabled=True,
            provider=resolved_provider,
            model=resolved_model,
            dimension=resolved_dimension,
            reason="provider_not_registered",
        )
    try:
        provider_client = get_provider(resolved_provider)
    except Exception:  # noqa: BLE001 - bootstrap falls back to lexical retrieval
        return _runtime(
            enabled=True,
            provider=resolved_provider,
            model=resolved_model,
            dimension=resolved_dimension,
            reason="provider_not_registered",
        )
    if not bool(getattr(provider_client, "supports_embeddings", False)):
        return _runtime(
            enabled=True,
            provider=resolved_provider,
            model=resolved_model,
            dimension=resolved_dimension,
            reason="provider_lacks_embeddings",
        )

    embedding_provider = LLMServiceEmbeddingProvider(
        llm_service,
        cache=cache,
        provider=resolved_provider,
        model=resolved_model,
        dimension=resolved_dimension,
    )
    return _runtime(
        enabled=True,
        provider=resolved_provider,
        model=resolved_model,
        dimension=resolved_dimension,
        reason="ready",
        embedding_provider=embedding_provider,
    )


__all__ = [
    "KnowledgeEmbeddingRuntime",
    "KnowledgeEmbeddingStatus",
    "resolve_knowledge_embedding_runtime",
]
