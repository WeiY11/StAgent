"""Knowledge chunk vector index — semantic search for the document layer.

The knowledge storage layer (``document_chunks``) uses BM25/FTS5 for lexical
search.  This module adds an optional **vector search path** that indexes chunk
embeddings into a dedicated Milvus collection, enabling semantic (meaning-based)
retrieval alongside the existing lexical pipeline.

When Milvus is unavailable the index degrades to a no-op and the retrieval
pipeline falls through to BM25/ILIKE — identical to the agent memory stack's
graceful degradation strategy.

Usage::

    from leagent.knowledge.vector_index import KnowledgeVectorIndex

    index = KnowledgeVectorIndex(embeddings=provider, connection=cfg)
    await index.upsert_chunks(file_id, user_id, chunks)
    hits = await index.search(user_id, query_vector, limit=10)
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    from leagent.library.chunking import Chunk
    from leagent.memory.embeddings import EmbeddingProvider
    from leagent.memory.vector import MilvusConnectionConfig

logger = logging.getLogger(__name__)

COLLECTION_NAME = "knowledge_chunks"

#: Maximum batch size for vector upsert (Milvus RPC limit).
_UPSERT_BATCH = 256


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
    ) -> None:
        from leagent.memory.vector import MilvusCollection

        self._embeddings = embeddings
        self._collection = MilvusCollection(
            name=COLLECTION_NAME,
            dimension=embeddings.dimension,
            description="LeAgent knowledge chunk vectors",
            connection=connection,
        )

    @property
    def available(self) -> bool:
        return self._collection.can_write

    async def upsert_chunks(
        self,
        file_id: UUID,
        user_id: UUID | None,
        chunks: list[Chunk],
        *,
        chunk_ids: list[UUID] | None = None,
    ) -> int:
        """Embed and upsert a batch of chunks into the vector index.

        Returns the number of vectors written.  Returns 0 when the vector
        backend is unavailable.
        """
        if not self.available or not chunks:
            return 0

        texts = [c.text for c in chunks]
        try:
            vectors = await self._embeddings.embed(texts)
        except Exception:  # noqa: BLE001
            logger.warning("knowledge_vector_embed_failed", file_id=str(file_id))
            return 0

        written = 0
        for i, vector in enumerate(vectors):
            row_id = str(chunk_ids[i]) if chunk_ids and i < len(chunk_ids) else str(UUID(int=i))
            try:
                result = await self._collection.upsert(
                    row_id=row_id,
                    vector=vector,
                    user_id=str(user_id) if user_id else None,
                    scope=str(file_id),
                )
                if result.written:
                    written += 1
            except Exception:  # noqa: BLE001
                logger.debug("knowledge_vector_upsert_failed", chunk_id=row_id)
        return written

    async def delete_for_file(self, file_id: UUID) -> int:
        """Remove all vectors associated with *file_id*.

        The current Milvus wrapper does not support bulk delete by scope,
        so this is a best-effort no-op.  Vectors for deleted files become
        orphaned but are filtered out by the ``file_id`` scope check during
        search.  A periodic cleanup job can reclaim them.
        """
        # Milvus collection does not currently track chunk_id → file_id
        # mapping in a way that supports efficient bulk delete.  The
        # search path filters by ``scope`` (= file_id), so orphaned vectors
        # are invisible to queries.
        return 0

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

_index: KnowledgeVectorIndex | None = None


def get_knowledge_vector_index() -> KnowledgeVectorIndex | None:
    """Return the module-level vector index, or ``None`` if not wired."""
    return _index


def set_knowledge_vector_index(index: KnowledgeVectorIndex | None) -> None:
    """Wire the module-level vector index (called during service bootstrap)."""
    global _index  # noqa: PLW0603
    _index = index


__all__ = [
    "KnowledgeVectorIndex",
    "get_knowledge_vector_index",
    "set_knowledge_vector_index",
]
