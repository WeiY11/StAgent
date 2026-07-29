"""Unified knowledge retrieval pipeline for document chunk search.

Consolidates the previously scattered search logic from:

- ``api/v1/documents.py`` (search_documents endpoint)
- ``tools/doc/knowledge_search.py`` (agent tool)
- ``db/repositories/document_chunks.py`` (repository layer)

Into a single ``KnowledgeRetrievalPipeline`` that:

1. Performs BM25/FTS5 search via the repository layer
2. Falls back to trigram for CJK queries
3. Falls back to ILIKE for PostgreSQL / when FTS is unavailable
4. Batch-validates file metadata (no N+1 queries)
5. Supports generation-scoped result caching (P2-3.4)

Both the REST API and the agent tool should delegate to this pipeline
rather than duplicating search + validation logic.
"""

from __future__ import annotations

import logging
from copy import deepcopy
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlmodel import select

if TYPE_CHECKING:
    from leagent.db.service import DatabaseService
    from leagent.services.cache.service import CacheService

logger = logging.getLogger(__name__)

#: Sentinel for cache miss.
_MISS: object = object()


def _is_uuid(value: str) -> bool:
    """Quick check if a string looks like a UUID."""
    if len(value) not in (32, 36):
        return False
    try:
        UUID(value)
        return True
    except (ValueError, AttributeError):
        return False

#: Default search limit.
DEFAULT_SEARCH_LIMIT = 20
#: Maximum allowed search limit.
MAX_SEARCH_LIMIT = 100


@dataclass(frozen=True)
class RetrievalResult:
    """A single chunk-level knowledge search result."""

    file_id: UUID
    chunk_id: UUID
    file_name: str
    score: float
    snippet: str
    start_offset: int
    end_offset: int
    seq: int
    text: str


@dataclass
class SearchOptions:
    """Tuning knobs for :meth:`KnowledgeRetrievalPipeline.search`."""

    query: str
    user_id: UUID | None = None
    limit: int = DEFAULT_SEARCH_LIMIT
    library_scope: str = "knowledge"
    file_type_filter: list[str] | None = None
    folder_id: UUID | None = None


class KnowledgeRetrievalPipeline:
    """Unified knowledge search: BM25 + trigram CJK fallback + lexical ILIKE.

    Instances are lightweight and stateless — safe to construct once and reuse
    across requests.  The optional ``cache`` parameter enables generation-scoped
    result caching identical to the memory recall pipeline.
    """

    def __init__(
        self,
        db: DatabaseService,
        *,
        cache: CacheService | None = None,
        generation_getter: Any | None = None,
    ) -> None:
        self._db = db
        self._cache = cache
        self._generation_getter = generation_getter

    def _cache_key(
        self, options: SearchOptions, generation: int
    ) -> str | None:
        """Build a deterministic cache key from search options."""
        if self._cache is None:
            return None
        from leagent.services.cache.keys import build_scoped_cache_key

        return build_scoped_cache_key(
            "kb_search",
            owner_id=options.user_id,
            generation=generation,
            vary={
                "query": options.query,
                "limit": options.limit,
                "scope": options.library_scope,
                "types": ",".join(sorted(options.file_type_filter or [])),
                "folder": str(options.folder_id) if options.folder_id else None,
            },
        )

    async def search(self, options: SearchOptions) -> list[RetrievalResult]:
        """Execute a knowledge search and return validated results.

        Combines BM25/FTS5 chunk search with trigram CJK fallback and
        batch file validation.  Results are cached when a
        :class:`CacheService` is configured.
        """
        query = (options.query or "").strip()
        if not query:
            return []

        options = SearchOptions(
            query=query,
            user_id=options.user_id,
            limit=max(1, min(options.limit, MAX_SEARCH_LIMIT)),
            library_scope=options.library_scope,
            file_type_filter=options.file_type_filter,
            folder_id=options.folder_id,
        )

        # Check cache.
        cache_key: str | None = None
        if self._cache is not None:
            generation = 0
            if self._generation_getter is not None:
                try:
                    generation = await self._generation_getter(options)
                except Exception:  # noqa: BLE001
                    pass
            cache_key = self._cache_key(options, generation)
            if cache_key is not None:
                hit = await self._cache.get(
                    cache_key, namespace="kb_search", default=_MISS
                )
                if hit is not _MISS:
                    return deepcopy(hit)

        results = await self._execute_search(options)

        # Cache the result.
        if cache_key is not None and self._cache is not None:
            await self._cache.set(cache_key, results, namespace="kb_search")

        return results

    async def _execute_search(
        self, options: SearchOptions
    ) -> list[RetrievalResult]:
        """Run the actual search pipeline (no caching).

        Performs **hybrid retrieval**: lexical (BM25 + trigram + ILIKE) and
        semantic (vector similarity) results are merged and de-duplicated,
        with vector hits receiving a configurable boost.
        """
        from leagent.db.sqlite_compat import (
            same_user_id,
            uuid_column_equals,
            uuid_column_in,
        )

        # Step 1: Lexical chunk-level search (BM25 → trigram → ILIKE).
        chunk_hits = await self._db.repositories.document_chunks.search(
            options.user_id,
            options.query,
            limit=options.limit * 4,
            library_scope=options.library_scope,
        )

        # Step 1b: Vector similarity search (optional, graceful degradation).
        vector_hits: dict[str, float] = {}  # chunk_id_str → score
        try:
            from leagent.knowledge.vector_index import get_knowledge_vector_index

            vi = get_knowledge_vector_index()
            if vi is not None and vi.available:
                v_results = await vi.search(
                    options.user_id,
                    options.query,
                    limit=options.limit * 2,
                )
                vector_hits = dict(v_results)
        except Exception:  # noqa: BLE001
            pass  # Vector unavailable — lexical-only is fine.

        if not chunk_hits and not vector_hits:
            return []

        # Step 2: Batch-validate file metadata.
        file_ids = list({hit.file_id for hit in chunk_hits})
        # Also include file_ids from vector hits (need to resolve chunk_id → file_id).
        if vector_hits:
            chunk_id_uuids = [UUID(cid) for cid in vector_hits if _is_uuid(cid)]
            if chunk_id_uuids:
                from leagent.db.models.document_chunk import DocumentChunk

                async with self._db.session() as session:
                    vc_result = await session.exec(
                        select(DocumentChunk.file_id, DocumentChunk.id).where(
                            uuid_column_in(session, DocumentChunk.id, chunk_id_uuids)
                        )
                    )
                    for file_id_val, _chunk_id_val in vc_result.all():
                        file_ids.append(file_id_val)
            file_ids = list(set(file_ids))

        files_by_id: dict[UUID, Any] = {}
        if file_ids:
            from leagent.db.models.file import File, LibraryScope

            async with self._db.session() as session:
                batch_stmt = (
                    select(File)
                    .where(File.is_deleted == False)  # noqa: E712
                    .where(
                        uuid_column_equals(
                            session, File.user_id, options.user_id
                        )
                    )
                    .where(File.library_scope == LibraryScope.KNOWLEDGE)
                    .where(File.is_indexed == True)  # noqa: E712
                    .where(uuid_column_in(session, File.id, file_ids))
                )
                result = await session.exec(batch_stmt)
                for f in result.all():
                    files_by_id[f.id] = f

        # Step 3: Build validated results from lexical hits.
        seen_chunk_ids: set[UUID] = set()
        results: list[RetrievalResult] = []
        for hit in chunk_hits:
            f = files_by_id.get(hit.file_id)
            if f is None:
                continue
            if (
                options.file_type_filter
                and hasattr(f.file_type, "value")
                and f.file_type.value not in options.file_type_filter
            ):
                continue
            if options.folder_id is not None and not same_user_id(
                f.folder_id, options.folder_id
            ):
                continue
            # Boost score if this chunk also appeared in vector results.
            boost = vector_hits.get(str(hit.chunk_id), 0.0)
            combined_score = hit.score + boost * 0.3  # 30% vector weight
            results.append(
                RetrievalResult(
                    file_id=hit.file_id,
                    chunk_id=hit.chunk_id,
                    file_name=hit.file_name,
                    score=combined_score,
                    snippet=hit.snippet,
                    start_offset=hit.start_offset,
                    end_offset=hit.end_offset,
                    seq=hit.seq,
                    text=hit.text,
                )
            )
            seen_chunk_ids.add(hit.chunk_id)
            if len(results) >= options.limit:
                break

        # Step 4: Append vector-only hits not already in lexical results.
        if vector_hits and len(results) < options.limit:
            await self._append_vector_only_hits(
                results, seen_chunk_ids, vector_hits, files_by_id, options
            )

        # Sort by combined score descending.
        results.sort(key=lambda r: r.score, reverse=True)
        return results[: options.limit]

    async def _append_vector_only_hits(
        self,
        results: list[RetrievalResult],
        seen_chunk_ids: set[UUID],
        vector_hits: dict[str, float],
        files_by_id: dict[UUID, Any],
        options: SearchOptions,
    ) -> None:
        """Add vector-only hits that weren't captured by lexical search."""
        from leagent.db.models.document_chunk import DocumentChunk
        from leagent.db.sqlite_compat import uuid_column_in

        unseen_ids = [
            UUID(cid)
            for cid in vector_hits
            if _is_uuid(cid) and UUID(cid) not in seen_chunk_ids
        ]
        if not unseen_ids:
            return

        async with self._db.session() as session:
            vc_result = await session.exec(
                select(DocumentChunk).where(
                    uuid_column_in(session, DocumentChunk.id, unseen_ids)
                )
            )
            chunks_by_id = {c.id: c for c in vc_result.all()}

        for cid_str, score in vector_hits.items():
            if len(results) >= options.limit:
                break
            if not _is_uuid(cid_str):
                continue
            cid = UUID(cid_str)
            if cid in seen_chunk_ids:
                continue
            chunk = chunks_by_id.get(cid)
            if chunk is None:
                continue
            f = files_by_id.get(chunk.file_id)
            if f is None:
                continue
            results.append(
                RetrievalResult(
                    file_id=chunk.file_id,
                    chunk_id=chunk.id,
                    file_name=getattr(f, "original_name", "") or f.name,
                    score=score * 0.3,
                    snippet=chunk.text[:200] if chunk.text else "",
                    start_offset=chunk.start_offset,
                    end_offset=chunk.end_offset,
                    seq=chunk.seq,
                    text=chunk.text,
                )
            )
            seen_chunk_ids.add(cid)


# Module-level singleton accessor.
_pipeline: KnowledgeRetrievalPipeline | None = None


def get_knowledge_retrieval_pipeline() -> KnowledgeRetrievalPipeline | None:
    """Return the module-level pipeline instance, or ``None`` if not wired."""
    return _pipeline


def set_knowledge_retrieval_pipeline(
    pipeline: KnowledgeRetrievalPipeline,
) -> None:
    """Wire the module-level pipeline (called during service bootstrap)."""
    global _pipeline  # noqa: PLW0603
    _pipeline = pipeline


__all__ = [
    "KnowledgeRetrievalPipeline",
    "RetrievalResult",
    "SearchOptions",
    "get_knowledge_retrieval_pipeline",
    "set_knowledge_retrieval_pipeline",
]
