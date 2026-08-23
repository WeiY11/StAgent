"""Knowledge chunk vector index — semantic search for the document layer.

The knowledge storage layer (``document_chunks``) uses BM25/FTS5 for lexical
search.  This module adds an optional **vector search path** that indexes chunk
embeddings into a dedicated Milvus collection, enabling semantic (meaning-based)
retrieval alongside the existing lexical pipeline.

When Milvus is unavailable the index degrades to a no-op and the retrieval
pipeline falls through to BM25/ILIKE — identical to the agent memory stack's
graceful degradation strategy.

Usage::

    from StAgent.knowledge.vector_index import KnowledgeVectorIndex

    index = KnowledgeVectorIndex(embeddings=provider, connection=cfg)
    await index.upsert_chunks(file_id, user_id, chunks)
    hits = await index.search(user_id, query_vector, limit=10)
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from typing import TYPE_CHECKING
from uuid import NAMESPACE_URL, UUID, uuid5

from StAgent.knowledge.vector_backend import canonical_index_version

if TYPE_CHECKING:
    from collections.abc import Sequence

    from StAgent.library.chunking import Chunk
    from StAgent.memory.embeddings import EmbeddingProvider
    from StAgent.memory.vector import MilvusCollection, MilvusConnectionConfig

logger = logging.getLogger(__name__)

COLLECTION_NAME = "knowledge_chunks"
DEFAULT_INDEX_VERSION = "rag-v1"

#: Maximum batch size for vector upsert (Milvus RPC limit).
_UPSERT_BATCH = 256


def _collection_name_for_version(index_version: str) -> str:
    """Return a stable Milvus-safe collection name for an index version."""
    version = canonical_index_version(index_version, default=DEFAULT_INDEX_VERSION)
    if version is None:
        raise ValueError("invalid index version")
    if version == DEFAULT_INDEX_VERSION:
        # Preserve the original collection for the default rollout.
        return COLLECTION_NAME
    normalized = re.sub(r"[^a-zA-Z0-9_]+", "_", version).strip("_").lower() or "version"
    digest = hashlib.sha256(version.encode("utf-8")).hexdigest()[:8]
    return f"{COLLECTION_NAME}_{normalized[:40]}_{digest}"


class KnowledgeVectorIndex:
    """Milvus-backed vector index for knowledge document chunks.

    The index stores chunk embeddings keyed by ``chunk_id`` with ``file_id``
    and ``user_id`` scalar filters, enabling hybrid (lexical + semantic) search
    in the :class:`KnowledgeRetrievalPipeline`.
    """

    def __init__(
        self,
        *,
        embeddings: EmbeddingProvider,
        connection: MilvusConnectionConfig | None = None,
        index_version: str = DEFAULT_INDEX_VERSION,
    ) -> None:
        self._embeddings = embeddings
        self._connection = connection
        canonical = canonical_index_version(index_version, default=DEFAULT_INDEX_VERSION)
        if canonical is None:
            raise ValueError("invalid index version")
        self._index_version = canonical
        self._collections: dict[str, MilvusCollection] = {}
        self._collection = self._collection_for_version(self._index_version)
        self._collection_name = _collection_name_for_version(self._index_version)

    def _collection_for_version(self, index_version: str) -> MilvusCollection:
        """Lazily construct an isolated collection for an active or candidate version."""
        version = canonical_index_version(index_version)
        if version is None:
            raise ValueError("invalid index version")
        existing = self._collections.get(version)
        if existing is not None:
            return existing
        from StAgent.memory.vector import MilvusCollection

        collection = MilvusCollection(
            name=_collection_name_for_version(version),
            dimension=self._embeddings.dimension,
            description="StAgent knowledge chunk vectors",
            connection=self._connection,
        )
        self._collections[version] = collection
        return collection

    def _selected_collection(self, index_version: str | None) -> MilvusCollection | None:
        """Resolve a version while retaining isolated legacy test construction."""
        active = getattr(self, "_index_version", DEFAULT_INDEX_VERSION)
        version = canonical_index_version(index_version, default=active)
        if version is None:
            return None
        collections = getattr(self, "_collections", None)
        if collections is not None:
            return collections.get(version)
        if version == active:
            return getattr(self, "_collection", None)
        return None

    @property
    def available(self) -> bool:
        return self._collection.can_write

    @property
    def index_version(self) -> str:
        """Configured index version represented by this vector collection."""
        return self._index_version

    @property
    def collection_name(self) -> str:
        """Resolved Milvus collection name used by this index."""
        return self._collection_name

    async def upsert_chunks(
        self,
        file_id: UUID,
        user_id: UUID | None,
        chunks: Sequence[Chunk],
        *,
        chunk_ids: Sequence[UUID] | None = None,
        index_version: str | None = None,
    ) -> int:
        """Embed and upsert a batch of chunks into the vector index.

        Returns the number of vectors written.  Returns 0 when the vector
        backend is unavailable.
        """
        collection = self._selected_collection(index_version)
        if not chunks or collection is None or not collection.can_write:
            return 0

        texts = [c.text for c in chunks]
        try:
            vectors = await self._embeddings.embed(texts)
        except Exception:  # noqa: BLE001
            logger.warning("knowledge_vector_embed_failed file_id=%s", file_id)
            return 0

        written = 0
        for i, vector in enumerate(vectors):
            row_id = (
                str(chunk_ids[i])
                if chunk_ids and i < len(chunk_ids)
                else str(uuid5(NAMESPACE_URL, f"StAgent:knowledge:{file_id}:{i}"))
            )
            try:
                result = await collection.upsert(
                    row_id=row_id,
                    vector=vector,
                    user_id=str(user_id) if user_id else None,
                    scope=str(file_id),
                )
                if result.written:
                    written += 1
            except Exception:  # noqa: BLE001
                logger.debug("knowledge_vector_upsert_failed chunk_id=%s", row_id)
        return written

    async def delete_for_file(
        self, file_id: UUID, *, index_version: str | None = None
    ) -> int:
        """Remove all vectors associated with *file_id*.

        The SQL chunk table remains the source of truth, but reindexing must
        remove obsolete vectors so they do not consume search capacity or
        survive document deletion indefinitely.
        """
        collection = self._selected_collection(index_version)
        if collection is None or not collection.can_write:
            return 0
        try:
            return await collection.delete_scope(str(file_id))
        except Exception:  # noqa: BLE001 - vector cleanup remains best-effort
            logger.debug("knowledge_vector_scope_delete_failed file_id=%s", file_id)
            return 0

    async def embed_query(self, query: str) -> list[float]:
        """Encode a query for the backend-neutral retrieval adapter."""
        return await self._embeddings.embed_one(query)

    async def search_vector(
        self,
        query_vector: Sequence[float],
        *,
        limit: int,
        allowed_file_ids: Sequence[UUID],
        index_version: str | None = None,
    ) -> list[tuple[str, float]]:
        """Search a precomputed vector when the caller has one file scope.

        The legacy Milvus collection has no safe large ACL ``IN`` predicate.
        Local sqlite-vec therefore performs pre-filtering; Milvus keeps the
        established SQL post-search ACL validation in the retrieval pipeline.
        """
        collection = self._selected_collection(index_version)
        if collection is None or not collection.can_write:
            return []
        if len(allowed_file_ids) != 1:
            return []
        try:
            return await collection.search(
                vector=list(query_vector),
                limit=limit,
                user_id=None,
                scope=str(allowed_file_ids[0]),
            )
        except Exception:  # noqa: BLE001
            logger.warning("knowledge_vector_search_failed")
            return []

    async def begin_version(self, index_version: str) -> bool:
        """Create a candidate collection without changing the active collection."""
        version = canonical_index_version(index_version)
        if not version or version == self._index_version:
            return False
        try:
            return self._collection_for_version(version).can_write
        except Exception:  # noqa: BLE001
            logger.warning("knowledge_vector_candidate_begin_failed")
            return False

    async def activate_version(self, index_version: str) -> bool:
        """Atomically direct subsequent requests to a prepared candidate."""
        version = canonical_index_version(index_version)
        if version is None or version == self._index_version:
            return False
        collection = self._collections.get(version)
        if collection is None or not collection.can_write:
            return False
        self._index_version = version
        self._collection = collection
        self._collection_name = _collection_name_for_version(version)
        return True

    async def discard_version(self, index_version: str) -> bool:
        """Drop a never-activated candidate collection when the client exposes it."""
        version = canonical_index_version(index_version)
        if version is None or version == self._index_version or version not in self._collections:
            return False
        collection = self._collections[version]
        remote_collection = getattr(collection, "_collection", None)
        drop = getattr(remote_collection, "drop", None)
        if callable(drop):
            try:
                await asyncio.to_thread(drop)
            except Exception:  # noqa: BLE001
                logger.warning("knowledge_vector_candidate_discard_failed")
                return False
        del self._collections[version]
        return True

    async def search(
        self,
        user_id: UUID | None,
        query: str,
        *,
        limit: int = 20,
        file_id: UUID | None = None,
    ) -> list[tuple[str, float]]:
        """Semantic search: embed *query* and return nearest chunk IDs.

        Returns a list of ``(chunk_id_str, score)`` tuples.  Returns empty
        list when the vector backend is unavailable.
        """
        if not self.available:
            return []

        try:
            query_vector = await self._embeddings.embed_one(query)
        except Exception:  # noqa: BLE001
            logger.warning("knowledge_vector_query_embed_failed")
            return []

        try:
            hits = await self._collection.search(
                vector=query_vector,
                limit=limit,
                user_id=str(user_id) if user_id else None,
                scope=str(file_id) if file_id else None,
            )
            return hits
        except Exception:  # noqa: BLE001
            logger.warning("knowledge_vector_search_failed")
            return []


# ── Module-level singleton ──────────────────────────────────────────

if TYPE_CHECKING:
    from StAgent.knowledge.vector_backend import KnowledgeVectorIndexBackend

_index: KnowledgeVectorIndexBackend | None = None


def get_knowledge_vector_index() -> KnowledgeVectorIndexBackend | None:
    """Return the module-level vector index, or ``None`` if not wired."""
    return _index


def set_knowledge_vector_index(index: KnowledgeVectorIndexBackend | None) -> None:
    """Wire the module-level vector index (called during service bootstrap)."""
    global _index  # noqa: PLW0603
    _index = index


__all__ = [
    "KnowledgeVectorIndex",
    "get_knowledge_vector_index",
    "set_knowledge_vector_index",
]
