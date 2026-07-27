"""Embedding provider protocol used by the agent memory stack.

The cognitive stores (episodic, semantic, procedural) all need to turn
arbitrary text into dense vectors. Rather than letting each store pull in
:class:`LLMService` directly, they talk to an :class:`EmbeddingProvider`.

``LLMServiceEmbeddingProvider`` is the production implementation: it
delegates to :meth:`LLMService.embed` and coalesces identical ``(model, text)``
lookups through the shared :class:`CacheService` when one is wired in.

``NullEmbeddingProvider`` returns zero vectors, which disables semantic
search but lets the BM25 / ILIKE fallback path continue to work.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from leagent.services.cache.keys import build_scoped_cache_key

if TYPE_CHECKING:
    from leagent.llm import LLMService
    from leagent.services.cache.service import CacheService

logger = logging.getLogger(__name__)


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Async-only embedding provider used by memory stores."""

    dimension: int

    async def embed(self, texts: Iterable[str]) -> list[list[float]]:
        ...

    async def embed_one(self, text: str) -> list[float]:
        ...


class NullEmbeddingProvider:
    """No-op provider that returns zero vectors."""

    def __init__(self, dimension: int = 1024) -> None:
        self.dimension = dimension
        self.last_degraded = True
        self.last_error: str | None = "null_embedding_provider"

    async def embed(self, texts: Iterable[str]) -> list[list[float]]:
        return [[0.0] * self.dimension for _ in texts]

    async def embed_one(self, text: str) -> list[float]:
        return [0.0] * self.dimension


class LLMServiceEmbeddingProvider:
    """Production embedding provider backed by :class:`LLMService`.

    Identical ``(model, text, dimension)`` embeddings are coalesced through the
    shared :class:`CacheService` (namespace ``embeddings``) when one is wired
    in, so repeat lookups never re-hit the backend. A per-instance batch lock
    also collapses concurrent identical requests onto a single provider call.
    Never logs text or vector values.
    """

    def __init__(
        self,
        llm_service: LLMService,
        *,
        cache: CacheService | None = None,
        model: str | None = None,
        dimension: int = 1024,
    ) -> None:
        self._llm = llm_service
        self._cache = cache
        self._model = model
        self.dimension = dimension
        self.last_degraded = False
        self.last_error: str | None = None
        self._batch_lock = asyncio.Lock()

    @property
    def model(self) -> str:
        return self._model or "default"

    async def _embed_uncached(self, texts: list[str]) -> list[list[float]]:
        try:
            response = await self._llm.embed(texts, model=self._model)
        except Exception as exc:  # noqa: BLE001
            logger.warning("embedding_backend_failed: %s", exc)
            self.last_degraded = True
            self.last_error = str(exc)
            raise RuntimeError(f"embedding_backend_failed: {exc}") from exc
        vectors = list(response.embeddings or [])
        if len(vectors) != len(texts):
            self.last_degraded = True
            self.last_error = "embedding_response_incomplete"
            raise RuntimeError("embedding_response_incomplete")
        if vectors and len(vectors[0]) != self.dimension:
            self.dimension = len(vectors[0])
        self.last_degraded = False
        self.last_error = None
        return vectors

    def _cache_key(self, text: str) -> str:
        return build_scoped_cache_key(
            "embedding",
            resource_id=self.model,
            vary={"text": text, "dimension": self.dimension},
        )

    async def embed(self, texts: Iterable[str]) -> list[list[float]]:
        items = [t or "" for t in texts]
        if not items:
            return []
        if self._cache is None:
            return await self._embed_uncached(items)

        keys = [self._cache_key(text) for text in items]
        cached = await self._cache.get_many(keys, namespace="embeddings")
        async with self._batch_lock:
            missing_keys = [key for key in dict.fromkeys(keys) if key not in cached]
            if missing_keys:
                # Re-check under the lock: a concurrent batch may have filled
                # these keys while we waited, collapsing identical requests
                # onto a single backend call.
                refreshed = await self._cache.get_many(
                    missing_keys, namespace="embeddings"
                )
                cached.update(refreshed)
                missing_keys = [key for key in missing_keys if key not in cached]
            if missing_keys:
                text_for_key = dict(zip(keys, items, strict=True))
                vectors = await self._embed_uncached(
                    [text_for_key[key] for key in missing_keys]
                )
                writes = dict(zip(missing_keys, vectors, strict=True))
                await self._cache.set_many(writes, namespace="embeddings")
                cached.update(writes)
        return [list(cached[key]) for key in keys]

    async def embed_one(self, text: str) -> list[float]:
        result = await self.embed([text])
        if not result:
            raise RuntimeError("embedding_empty")
        return result[0]


__all__ = [
    "EmbeddingProvider",
    "LLMServiceEmbeddingProvider",
    "NullEmbeddingProvider",
]
