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
import importlib
import logging
import math
from pathlib import Path  # noqa: TC003
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from leagent.services.cache.keys import build_scoped_cache_key

if TYPE_CHECKING:
    from collections.abc import Iterable

    from leagent.llm import LLMService
    from leagent.services.cache.service import CacheService

logger = logging.getLogger(__name__)


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Async-only embedding provider used by memory stores."""

    dimension: int

    async def embed(self, texts: Iterable[str]) -> list[list[float]]: ...

    async def embed_one(self, text: str) -> list[float]: ...


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


class LocalSentenceTransformerEmbeddingProvider:
    """Offline-only, lazily loaded SentenceTransformers embedding provider."""

    def __init__(
        self,
        model_path: str | Path,
        *,
        dimension: int,
        query_prompt: str | None = None,
        document_prompt: str | None = None,
        normalize: bool = True,
    ) -> None:
        if dimension <= 0:
            raise ValueError("dimension must be positive")
        self.model_path = Path(model_path)
        self.dimension = dimension
        self.query_prompt = query_prompt
        self.document_prompt = document_prompt
        self.normalize = normalize
        self._model: object | None = None
        self._load_lock = asyncio.Lock()
        self._encode_lock = asyncio.Lock()
        self.last_degraded = False
        self.last_error: str | None = None

    async def embed(self, texts: Iterable[str]) -> list[list[float]]:
        return await self.embed_documents(texts)

    async def embed_one(self, text: str) -> list[float]:
        return await self.embed_query(text)

    async def embed_documents(self, texts: Iterable[str]) -> list[list[float]]:
        return await self._encode([text or "" for text in texts], mode="document")

    async def embed_query(self, text: str) -> list[float]:
        result = await self._encode([text or ""], mode="query")
        if not result:
            raise RuntimeError("embedding_empty")
        return result[0]

    async def _get_model(self) -> object:
        if self._model is not None:
            return self._model
        async with self._load_lock:
            if self._model is None:
                if not self.model_path.is_dir() or self.model_path.is_symlink():
                    raise RuntimeError("local_embedding_model_missing")
                try:
                    module = importlib.import_module("sentence_transformers")
                except ModuleNotFoundError as exc:
                    raise RuntimeError("sentence_transformers_rag_extra_required") from exc
                factory = module.SentenceTransformer
                self._model = await asyncio.to_thread(
                    factory,
                    str(self.model_path),
                    local_files_only=True,
                    trust_remote_code=False,
                )
        return self._model

    async def _encode(self, texts: list[str], *, mode: str) -> list[list[float]]:
        if not texts:
            return []
        model = await self._get_model()
        try:
            async with self._encode_lock:
                vectors = await asyncio.to_thread(self._encode_sync, model, texts, mode)
            checked = self._validate_vectors(vectors, len(texts))
        except Exception as exc:
            self.last_degraded = True
            self.last_error = str(exc)
            raise
        self.last_degraded = False
        self.last_error = None
        return checked

    def _encode_sync(self, model: object, texts: list[str], mode: str) -> object:
        kwargs: dict[str, object] = {"normalize_embeddings": self.normalize}
        preferred = "encode_query" if mode == "query" else "encode_document"
        method = getattr(model, preferred, None)
        prompt = self.query_prompt if mode == "query" else self.document_prompt
        if callable(method):
            if prompt:
                try:
                    return method(texts, prompt=prompt, **kwargs)
                except TypeError:
                    # Older SentenceTransformers task helpers do not accept a
                    # prompt.  Route through the generic compatible encoder
                    # so an administrator's explicit prompt is never ignored.
                    pass
            else:
                return method(texts, **kwargs)
        encoder = model.encode  # type: ignore[attr-defined]
        if prompt:
            try:
                return encoder(texts, prompt=prompt, **kwargs)
            except TypeError:
                texts = [f"{prompt}{text}" for text in texts]
        if callable(method) and not prompt:
            return method(texts, **kwargs)
        return encoder(texts, **kwargs)

    def _validate_vectors(self, raw: object, expected_count: int) -> list[list[float]]:
        try:
            vectors = raw.tolist() if hasattr(raw, "tolist") else raw
            checked = [[float(value) for value in vector] for vector in vectors]  # type: ignore[union-attr]
        except (TypeError, ValueError) as exc:
            raise RuntimeError("embedding_response_invalid") from exc
        if len(checked) != expected_count:
            raise RuntimeError("embedding_response_incomplete")
        for vector in checked:
            if len(vector) != self.dimension:
                raise RuntimeError("embedding_dimension_mismatch")
            if not all(math.isfinite(value) for value in vector):
                raise RuntimeError("embedding_vector_nonfinite")
        return checked


class LLMServiceEmbeddingProvider:
    """Production embedding provider backed by :class:`LLMService`.

    Identical ``(provider, model, text, dimension)`` embeddings are coalesced
    through the shared :class:`CacheService` (namespace ``embeddings``) when
    one is wired in, so repeat lookups never re-hit the backend. A per-instance
    batch lock also collapses concurrent identical requests onto a single
    provider call.
    Never logs text or vector values.
    """

    def __init__(
        self,
        llm_service: LLMService,
        *,
        cache: CacheService | None = None,
        provider: str | None = None,
        model: str | None = None,
        dimension: int = 1024,
    ) -> None:
        self._llm = llm_service
        self._cache = cache
        self._provider = provider or None
        self._model = model
        self.dimension = dimension
        self.last_degraded = False
        self.last_error: str | None = None
        self._batch_lock = asyncio.Lock()

    @property
    def model(self) -> str:
        model = self._model or "default"
        return f"{self._provider}:{model}" if self._provider else model

    @property
    def provider_name(self) -> str | None:
        """Explicit provider selected for embeddings, if any."""
        return self._provider

    async def _embed_uncached(self, texts: list[str]) -> list[list[float]]:
        try:
            kwargs: dict[str, str | None] = {"model": self._model}
            if self._provider:
                kwargs["provider"] = self._provider
            response = await self._llm.embed(texts, **kwargs)
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
                refreshed = await self._cache.get_many(missing_keys, namespace="embeddings")
                cached.update(refreshed)
                missing_keys = [key for key in missing_keys if key not in cached]
            if missing_keys:
                text_for_key = dict(zip(keys, items, strict=True))
                vectors = await self._embed_uncached([text_for_key[key] for key in missing_keys])
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
    "LocalSentenceTransformerEmbeddingProvider",
    "NullEmbeddingProvider",
]
