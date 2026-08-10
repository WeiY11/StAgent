"""Shared contracts for optional knowledge-vector projections.

The primary document database remains authoritative.  Backends in this module
only hold rebuildable projections and must degrade to lexical retrieval when
they cannot safely answer a query.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Sequence
    from uuid import UUID

    from leagent.library.chunking import Chunk


@runtime_checkable
class KnowledgeVectorIndexBackend(Protocol):
    """Rebuildable vector projection used by knowledge retrieval."""

    @property
    def available(self) -> bool: ...

    @property
    def index_version(self) -> str: ...

    async def upsert_chunks(
        self,
        file_id: UUID,
        user_id: UUID | None,
        chunks: Sequence[Chunk],
        *,
        chunk_ids: Sequence[UUID] | None = None,
        index_version: str | None = None,
    ) -> int: ...

    async def delete_for_file(
        self, file_id: UUID, *, index_version: str | None = None
    ) -> int: ...

    async def embed_query(self, query: str) -> list[float]: ...

    async def search_vector(
        self,
        query_vector: Sequence[float],
        *,
        limit: int,
        allowed_file_ids: Sequence[UUID],
        index_version: str | None = None,
    ) -> list[tuple[str, float]]: ...

    async def begin_version(self, index_version: str) -> bool: ...

    async def activate_version(self, index_version: str) -> bool: ...

    async def discard_version(self, index_version: str) -> bool: ...


def version_table_name(index_version: str) -> str:
    """Return a SQL-safe stable name without exposing model identifiers."""
    import hashlib

    value = (index_version or "rag-v1").strip() or "rag-v1"
    return f"vec_{hashlib.sha256(value.encode('utf-8')).hexdigest()[:24]}"


__all__ = ["KnowledgeVectorIndexBackend", "version_table_name"]
