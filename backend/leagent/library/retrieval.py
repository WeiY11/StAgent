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
import time
from contextlib import suppress
from copy import deepcopy
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Literal
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
    space_id: UUID | None = None
    fusion_method: Literal["rrf", "score"] = "rrf"
    query_rewrite: bool = False
    conversation: list[dict[str, str]] | None = None
    rerank: bool = True


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
        query_rewriter: Any | None = None,
        reranker: Any | None = None,
        opensearch: Any | None = None,
    ) -> None:
        self._db = db
        self._cache = cache
        self._generation_getter = generation_getter
        self._index_version = "rag-v1"
        with suppress(Exception):
            from leagent.config.settings import get_settings

            self._index_version = get_settings().knowledge.index_version
        if query_rewriter is None:
            from leagent.knowledge.query import DeterministicQueryRewriter

            query_rewriter = DeterministicQueryRewriter()
        self._query_rewriter = query_rewriter
        if reranker is None:
            try:
                from leagent.config.settings import get_settings
                from leagent.knowledge.rerank import HttpRerankProvider

                cfg = get_settings().knowledge
                if cfg.rerank_endpoint:
                    reranker = HttpRerankProvider(
                        cfg.rerank_endpoint,
                        api_key=cfg.rerank_api_key,
                        timeout_seconds=cfg.rerank_timeout_seconds,
                    )
            except Exception:  # noqa: BLE001
                logger.debug("rerank_backend_not_configured", exc_info=True)
        if reranker is None:
            from leagent.knowledge.rerank import LexicalRerankProvider

            reranker = LexicalRerankProvider()
        self._reranker = reranker
        if opensearch is None:
            try:
                from leagent.config.settings import get_settings
                from leagent.knowledge.opensearch import OpenSearchKnowledgeIndex

                cfg = get_settings().knowledge
                if cfg.opensearch_url:
                    opensearch = OpenSearchKnowledgeIndex(
                        cfg.opensearch_url,
                        index=cfg.opensearch_index,
                        api_key=cfg.opensearch_api_key,
                        timeout_seconds=cfg.opensearch_timeout_seconds,
                    )
            except Exception:  # noqa: BLE001
                logger.debug("opensearch_backend_not_configured", exc_info=True)
        self._opensearch = opensearch

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
                "space": str(options.space_id) if options.space_id else None,
                "fusion": options.fusion_method,
                "rewrite": options.query_rewrite,
                "rerank": options.rerank,
                "conversation": str(options.conversation or "")[-2000:],
            },
        )

    async def search(self, options: SearchOptions) -> list[RetrievalResult]:
        """Execute retrieval and emit bounded latency/result metrics."""
        started = time.perf_counter()
        status = "success"
        results: list[RetrievalResult] = []
        try:
            results = await self._search_impl(options)
            if not results:
                status = "empty"
            return results
        except Exception:
            status = "error"
            raise
        finally:
            try:
                from leagent.utils.metrics import get_metrics

                get_metrics().record_knowledge_retrieval(
                    "hybrid",
                    time.perf_counter() - started,
                    len(results),
                    status=status,
                )
            except Exception:  # noqa: BLE001 - observability never breaks search
                logger.debug("knowledge_retrieval_metrics_failed", exc_info=True)

    async def _search_impl(self, options: SearchOptions) -> list[RetrievalResult]:
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
            space_id=options.space_id,
            fusion_method=options.fusion_method,
            query_rewrite=options.query_rewrite,
            conversation=options.conversation,
            rerank=options.rerank,
        )
        if not options.query_rewrite:
            with suppress(Exception):
                from leagent.config.settings import get_settings

                if get_settings().knowledge.query_rewrite_enabled:
                    options = replace(options, query_rewrite=True)

        # Check cache.
        cache_key: str | None = None
        if self._cache is not None:
            generation = 0
            if self._generation_getter is not None:
                with suppress(Exception):
                    generation = await self._generation_getter(options)
            cache_key = self._cache_key(options, generation)
            if cache_key is not None:
                hit = await self._cache.get(
                    cache_key, namespace="kb_search", default=_MISS
                )
                if hit is not _MISS:
                    return deepcopy(hit)

        if options.query_rewrite:
            rewrite = await self._query_rewriter.rewrite(
                query,
                conversation=options.conversation,
            )
            merged: dict[UUID, RetrievalResult] = {}
            fused_rank_score: dict[UUID, float] = {}
            for variant in rewrite.variants:
                variant_options = replace(options, query=variant, query_rewrite=False)
                for rank, result in enumerate(await self._execute_search(variant_options), start=1):
                    fused_rank_score[result.chunk_id] = fused_rank_score.get(result.chunk_id, 0.0) + 1.0 / (60 + rank)
                    previous = merged.get(result.chunk_id)
                    if previous is None or result.score > previous.score:
                        merged[result.chunk_id] = result
            results = [
                replace(item, score=fused_rank_score.get(item.chunk_id, item.score))
                for item in merged.values()
            ]
            results.sort(key=lambda item: item.score, reverse=True)
            results = results[: options.limit]
        else:
            results = await self._execute_search(options)

        if options.rerank and self._reranker is not None and results:
            try:
                original_results = results
                order = await self._reranker.rerank(
                    query,
                    [item.text for item in original_results],
                    top_n=min(options.limit, len(original_results)),
                )
                ranked_indices = [
                    index
                    for index, _ in order
                    if 0 <= index < len(original_results)
                ]
                selected = set(ranked_indices)
                results = [original_results[index] for index in ranked_indices]
                results.extend(
                    item
                    for index, item in enumerate(original_results)
                    if index not in selected
                )
                results = results[: options.limit]
            except Exception:  # noqa: BLE001
                logger.debug("knowledge_rerank_degraded", exc_info=True)

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
        from leagent.db.sqlite_compat import same_user_id, uuid_column_equals, uuid_column_in

        # Step 1: Lexical chunk-level search (BM25 → trigram → ILIKE).
        chunk_hits = await self._db.repositories.document_chunks.search(
            options.user_id,
            options.query,
            limit=options.limit * 4,
            library_scope=options.library_scope,
        )

        if self._opensearch is not None:
            try:
                try:
                    remote_hits = await self._opensearch.search(
                        options.query,
                        limit=options.limit * 4,
                        index_version=self._index_version,
                    )
                except TypeError:
                    # Keep test/custom adapters compatible with the original
                    # two-argument protocol while the built-in adapter uses
                    # the version filter to prevent stale index hits.
                    remote_hits = await self._opensearch.search(
                        options.query,
                        limit=options.limit * 4,
                    )
                chunk_hits.extend(remote_hits)
            except Exception:  # noqa: BLE001
                logger.debug("opensearch_retrieval_degraded", exc_info=True)

        # Step 1b: Vector similarity search (optional, graceful degradation).
        vector_hits: dict[str, float] = {}  # chunk_id_str → score
        try:
            from leagent.knowledge.vector_index import get_knowledge_vector_index

            vi = get_knowledge_vector_index()
            if vi is not None and vi.available:
                v_results = await vi.search(
                    # Milvus currently stores the uploader id, not the full
                    # organization/space ACL.  Do not pre-filter by owner or
                    # shared documents would disappear before the SQL ACL
                    # predicate validates their file metadata below.
                    None,
                    options.query,
                    limit=options.limit * 2,
                )
                vector_hits = dict(v_results)
        except Exception:  # noqa: BLE001
            pass  # Vector unavailable — lexical-only is fine.

        if not chunk_hits and not vector_hits:
            return []

        # Step 2: Batch-validate file metadata.
        fusion_scores = (
            _rrf_fusion_scores(chunk_hits, vector_hits)
            if options.fusion_method == "rrf"
            else {}
        )
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
            from leagent.knowledge.acl import knowledge_file_access_clause

            async with self._db.session() as session:
                batch_stmt = (
                    select(File)
                    .where(File.is_deleted == False)  # noqa: E712
                    .where(knowledge_file_access_clause(session, options.user_id))
                    .where(File.library_scope == LibraryScope.KNOWLEDGE)
                    .where(File.is_indexed == True)  # noqa: E712
                    .where(uuid_column_in(session, File.id, file_ids))
                )
                if options.space_id is not None:
                    batch_stmt = batch_stmt.where(
                        uuid_column_equals(session, File.knowledge_space_id, options.space_id)
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
            combined_score = fusion_scores.get(str(hit.chunk_id), hit.score + boost * 0.3)
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
                results,
                seen_chunk_ids,
                vector_hits,
                files_by_id,
                options,
                fusion_scores,
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
        fusion_scores: dict[str, float] | None = None,
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
                    score=(fusion_scores or {}).get(cid_str, score * 0.3),
                    snippet=chunk.text[:200] if chunk.text else "",
                    start_offset=chunk.start_offset,
                    end_offset=chunk.end_offset,
                    seq=chunk.seq,
                    text=chunk.text,
                )
            )
            seen_chunk_ids.add(cid)


def _rrf_fusion_scores(
    lexical_hits: list[Any],
    vector_hits: dict[str, float],
    *,
    k: int = 60,
) -> dict[str, float]:
    """Fuse independently ranked lexical/vector lists with reciprocal rank."""
    scores: dict[str, float] = {}
    for rank, hit in enumerate(lexical_hits, start=1):
        key = str(hit.chunk_id)
        scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
    for rank, (chunk_id, _score) in enumerate(vector_hits.items(), start=1):
        key = str(chunk_id)
        scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
    return scores


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
