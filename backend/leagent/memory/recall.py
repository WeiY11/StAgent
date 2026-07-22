"""Hybrid semantic + lexical recall across the three cognitive stores.

The pipeline combines three ranking signals:

1. **Semantic similarity** via Milvus. We embed the query once and fan out
   one ``search`` per store; the raw cosine scores become the base ranking.
2. **Lexical fallback** via SQLAlchemy ``ilike`` (works on SQLite and Postgres)
   when Milvus is unavailable
   or returns nothing useful. This is intentionally simple (no BM25 server
   dependency) but always available. When :attr:`RecallOptions.bm25_rerank`
   is enabled the flat lexical scores are replaced by an in-process
   :mod:`BM25 <leagent.memory.bm25>` relevance score so the strongest keyword
   matches surface first instead of the merely most-recent ones.
3. **Recency + quality rerank**: episodes and procedures with a more
   recent ``last_run_at`` / ``created_at`` get a modest boost, and
   :class:`RecallEntry` items pointing at procedures with a low success
   rate are demoted.

Finally the pipeline filters results against :class:`FileStateCache` so
the controller doesn't re-inject content already visible in the current
turn (e.g. a file the agent just read).
"""

from __future__ import annotations

import asyncio
import logging
import math
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import TYPE_CHECKING
from uuid import UUID

from leagent.memory.bm25 import rerank_entries as _bm25_rerank
from leagent.memory.types import MemoryKind, RecallBundle, RecallEntry
from leagent.services.cache.keys import build_scoped_cache_key

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from leagent.context.file_state import FileState as FileStateCache
    from leagent.memory.embeddings import EmbeddingProvider
    from leagent.memory.episodic import EpisodicStore
    from leagent.memory.procedural import ProceduralStore
    from leagent.memory.semantic import SemanticStore
    from leagent.services.cache.service import CacheService

logger = logging.getLogger(__name__)

#: Sentinel telling a cache hit apart from a genuinely cached falsy value.
_MISS: object = object()

DEFAULT_LIMIT_PER_STORE = 4
DEFAULT_TOTAL_LIMIT = 8
RECENCY_HALF_LIFE_DAYS = 14.0

# When BM25 reranking is on, the lexical scan fetches a wider candidate pool
# (``per_store_limit * _BM25_POOL_FACTOR`` rows) so relevance — not just
# recency — decides which entries survive the final trim to ``per_store_limit``.
_BM25_POOL_FACTOR = 8
# Lexical scores are rescaled into ``[0, _BM25_RESCALE]`` so they stay in the
# same band as the flat scores they replace and remain comparable to the
# semantic cosine scores during the cross-store merge.
_BM25_RESCALE = 0.5

# Strong refs to in-flight background recency-bookkeeping tasks so they are not
# garbage-collected before completion (recall result is returned immediately).
_BACKGROUND_NOTE_TASKS: set[asyncio.Task[None]] = set()


@dataclass(slots=True)
class RecallOptions:
    """Tuning knobs for :meth:`RetrievalPipeline.recall`."""

    query: str
    recall_anchor: str | None = None
    user_id: UUID | None = None
    session_id: UUID | None = None
    workspace_id: UUID | None = None
    limit: int = DEFAULT_TOTAL_LIMIT
    per_store_limit: int = DEFAULT_LIMIT_PER_STORE
    include_episodic: bool = True
    include_semantic: bool = True
    include_procedural: bool = True
    file_state: FileStateCache | None = None
    bm25_rerank: bool = False
    lexical_pool_limit: int | None = None


class RetrievalPipeline:
    """Orchestrates recall across the three cognitive stores."""

    def __init__(
        self,
        *,
        episodic: EpisodicStore,
        semantic: SemanticStore,
        procedural: ProceduralStore,
        embeddings: EmbeddingProvider,
        cache: CacheService | None = None,
        generation_getter: Callable[[RecallOptions], Awaitable[int]] | None = None,
    ) -> None:
        self._episodic = episodic
        self._semantic = semantic
        self._procedural = procedural
        self._embeddings = embeddings
        self._cache = cache
        self._generation_getter = generation_getter

    def _recall_cache_key(self, options: RecallOptions, generation: int) -> str | None:
        """Owner/generation-scoped recall key, or None when uncacheable.

        Current-turn ``file_state`` exclusions change the result set, so we only
        cache when their deterministic signature can be folded into ``vary``.
        """
        file_state_sig = self._file_state_signature(options.file_state)
        if file_state_sig is _MISS:
            return None
        return build_scoped_cache_key(
            "recall",
            owner_id=options.user_id,
            resource_id=options.workspace_id or options.session_id,
            generation=generation,
            vary={
                "query": options.query,
                "limit": options.limit,
                "per_store_limit": options.per_store_limit,
                "episodic": options.include_episodic,
                "semantic": options.include_semantic,
                "procedural": options.include_procedural,
                "bm25": options.bm25_rerank,
                "file_state": file_state_sig,
            },
        )

    @staticmethod
    def _file_state_signature(file_state: FileStateCache | None) -> object:
        """Deterministic signature of file-state exclusions.

        Returns ``None`` when there is nothing to exclude, a sorted path list
        when a stable one is available, or the ``_MISS`` sentinel when the
        signature cannot be computed (caller must then skip caching).
        """
        if file_state is None:
            return None
        try:
            paths = file_state.paths()
        except Exception:  # noqa: BLE001
            return _MISS
        if not paths:
            return None
        return sorted(str(p) for p in paths)

    async def recall(self, options: RecallOptions) -> RecallBundle:
        query = (options.query or "").strip()
        if not query and options.recall_anchor:
            query = (options.recall_anchor or "").strip()
        bundle = RecallBundle(query=query)
        if not query:
            return bundle

        options = replace(options, query=query)

        cache_key: str | None = None
        if self._cache is not None:
            generation = (
                await self._generation_getter(options)
                if self._generation_getter is not None
                else 0
            )
            cache_key = self._recall_cache_key(options, generation)
            if cache_key is not None:
                hit = await self._cache.get(
                    cache_key, namespace="recall", default=_MISS
                )
                if hit is not _MISS:
                    # Deep copy so ``note_recall`` bookkeeping or caller
                    # mutation never touches the resident cached bundle.
                    return deepcopy(hit)

        vector: list[float] | None = None
        if self._vector_search_enabled(options):
            try:
                vector = await self._embeddings.embed_one(query)
                # NullEmbeddingProvider and other degraded paths must not run cosine
                # search on poison or placeholder vectors.
                if getattr(self._embeddings, "last_degraded", False):
                    vector = None
            except Exception as exc:  # noqa: BLE001
                logger.warning("recall_embed_failed: %s", exc)
                vector = None

        async def _empty() -> list[RecallEntry]:
            return []

        ep_task = (
            self._episodic_candidates(options, vector)
            if options.include_episodic
            else _empty()
        )
        sem_task = (
            self._semantic_candidates(options, vector)
            if options.include_semantic and options.user_id is not None
            else _empty()
        )
        proc_task = (
            self._procedural_candidates(options, vector)
            if options.include_procedural
            else _empty()
        )
        ep_r, sem_r, proc_r = await asyncio.gather(ep_task, sem_task, proc_task)
        candidates: list[RecallEntry] = []
        candidates.extend(ep_r)
        candidates.extend(sem_r)
        candidates.extend(proc_r)

        ranked = self._rerank(candidates)
        collapsed = self._deduplicate_and_collapse(ranked, options.file_state)
        bundle.extend(collapsed[: max(1, options.limit)])

        episodic_ids = [
            entry.source_id
            for entry in bundle.entries
            if entry.kind is MemoryKind.EPISODIC
        ]
        if episodic_ids:
            # Recency bookkeeping sits on the per-turn critical path (recall is
            # consumed before the first LLM call). Fire it in the background so
            # the bundle is returned without waiting on these DB writes.
            async def _note_recalls(ids: list[UUID]) -> None:
                for source_id in ids:
                    try:
                        await self._episodic.note_recall(source_id)
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("recall_note_failed: %s", exc)

            task = asyncio.create_task(_note_recalls(episodic_ids))
            _BACKGROUND_NOTE_TASKS.add(task)
            task.add_done_callback(_BACKGROUND_NOTE_TASKS.discard)

        if cache_key is not None and self._cache is not None:
            # Store a deep copy so the returned bundle can be mutated freely
            # without corrupting resident cache state.
            await self._cache.set(
                cache_key, deepcopy(bundle), namespace="recall"
            )
        return bundle

    def _vector_search_enabled(self, options: RecallOptions) -> bool:
        stores: list[object] = []
        if options.include_episodic:
            stores.append(self._episodic)
        if options.include_semantic and options.user_id is not None:
            stores.append(self._semantic)
        if options.include_procedural:
            stores.append(self._procedural)

        collections = [
            getattr(store, "collection", None)
            for store in stores
            if getattr(store, "collection", None) is not None
        ]
        if not collections:
            # Unit-test fakes and custom stores may not expose vector health;
            # preserve the previous behavior for those implementations.
            return True
        return any(bool(getattr(collection, "can_search", False)) for collection in collections)

    # -- lexical candidate post-processing ------------------------------

    def _lexical_fetch_limit(self, options: RecallOptions) -> int:
        """Row count for the lexical scan: a wider pool when BM25 reranks."""
        if options.bm25_rerank:
            if options.lexical_pool_limit:
                return max(options.per_store_limit, options.lexical_pool_limit)
            return max(1, options.per_store_limit) * _BM25_POOL_FACTOR
        return options.per_store_limit

    def _maybe_bm25_rerank(
        self,
        results: list[RecallEntry],
        options: RecallOptions,
    ) -> list[RecallEntry]:
        """Rerank a lexical candidate pool by BM25 relevance and trim it.

        No-op when BM25 is disabled or the pool is empty. Otherwise replaces
        the flat lexical scores with normalised BM25 scores (most relevant
        first) and trims the widened pool back to ``per_store_limit``.
        """
        if not options.bm25_rerank or not results:
            return results
        reranked = _bm25_rerank(results, options.query, rescale_to=_BM25_RESCALE)
        return reranked[: max(1, options.per_store_limit)]

    # -- store-specific candidate gathering -----------------------------

    async def _episodic_candidates(
        self,
        options: RecallOptions,
        vector: list[float] | None,
    ) -> list[RecallEntry]:
        results: list[RecallEntry] = []
        if vector is not None:
            try:
                results.extend(
                    await self._episodic.semantic_search(
                        vector,
                        user_id=options.user_id,
                        session_id=options.session_id,
                        limit=options.per_store_limit,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("episodic_semantic_failed: %s", exc)
        if not results:
            try:
                results.extend(
                    await self._episodic.lexical_search(
                        options.query,
                        user_id=options.user_id,
                        session_id=options.session_id,
                        limit=self._lexical_fetch_limit(options),
                    )
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("episodic_lexical_failed: %s", exc)
        return self._maybe_bm25_rerank(results, options)

    async def _semantic_candidates(
        self,
        options: RecallOptions,
        vector: list[float] | None,
    ) -> list[RecallEntry]:
        if options.user_id is None:
            return []
        results: list[RecallEntry] = []
        if vector is not None:
            try:
                results.extend(
                    await self._semantic.semantic_search(
                        vector,
                        user_id=options.user_id,
                        workspace_id=options.workspace_id,
                        limit=options.per_store_limit,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("semantic_semantic_failed: %s", exc)
        if not results:
            try:
                results.extend(
                    await self._semantic.lexical_search(
                        options.query,
                        user_id=options.user_id,
                        workspace_id=options.workspace_id,
                        limit=self._lexical_fetch_limit(options),
                    )
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("semantic_lexical_failed: %s", exc)
        return self._maybe_bm25_rerank(results, options)

    async def _procedural_candidates(
        self,
        options: RecallOptions,
        vector: list[float] | None,
    ) -> list[RecallEntry]:
        results: list[RecallEntry] = []
        if vector is not None:
            try:
                results.extend(
                    await self._procedural.semantic_search(
                        vector,
                        user_id=options.user_id,
                        workspace_id=options.workspace_id,
                        limit=options.per_store_limit,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("procedural_semantic_failed: %s", exc)
        if not results:
            try:
                results.extend(
                    await self._procedural.lexical_search(
                        options.query,
                        user_id=options.user_id,
                        workspace_id=options.workspace_id,
                        limit=self._lexical_fetch_limit(options),
                    )
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("procedural_lexical_failed: %s", exc)
        return self._maybe_bm25_rerank(results, options)

    # -- ranking --------------------------------------------------------

    def _rerank(self, candidates: list[RecallEntry]) -> list[RecallEntry]:
        now = datetime.now(timezone.utc)
        # Timestamp strings repeat across entries (many memories share a
        # creation batch); parse each distinct value once.
        ts_cache: dict[str, datetime | None] = {}
        for entry in candidates:
            entry.score = _apply_boosts(entry, now=now, ts_cache=ts_cache)
        return sorted(candidates, key=lambda e: e.score, reverse=True)

    def _deduplicate_and_collapse(
        self,
        ranked: list[RecallEntry],
        file_state: FileStateCache | None,
    ) -> list[RecallEntry]:
        """Dedupe by ``(kind, source_id)`` + content signature, drop entries
        that mention files already open this turn, and collapse episodic
        entries whose content a semantic fact already covers.

        Fuses the former ``_deduplicate`` and ``_collapse_semantic_over_episodic``
        passes into one and computes each entry's content signature at most
        once (it was previously recomputed up to three times per entry).
        """
        sigs: dict[int, str] = {}
        semantic_sigs: set[str] = set()
        for entry in ranked:
            if entry.kind is MemoryKind.SEMANTIC:
                semantic_sigs.add(_signature_for(entry, sigs))
        has_semantic = bool(semantic_sigs)

        seen_ids: set[str] = set()
        seen_texts: set[str] = set()
        out: list[RecallEntry] = []
        for entry in ranked:
            id_key = f"{entry.kind.value}:{entry.source_id}"
            if id_key in seen_ids:
                continue
            seen_ids.add(id_key)
            if file_state is not None and _mentions_known_file(entry, file_state):
                continue
            sig = _signature_for(entry, sigs)
            if sig in seen_texts:
                continue
            seen_texts.add(sig)
            if (
                has_semantic
                and entry.kind is MemoryKind.EPISODIC
                and sig in semantic_sigs
            ):
                continue
            out.append(entry)
        return out

    def _deduplicate(
        self,
        ranked: list[RecallEntry],
        file_state: FileStateCache | None,
    ) -> list[RecallEntry]:
        """Backward-compatible dedupe pass (id + content signature + file-state
        filter) without the semantic-over-episodic collapse. The hot recall path
        uses the fused :meth:`_deduplicate_and_collapse`; this is retained for
        callers and tests that exercise the dedupe step in isolation."""
        sigs: dict[int, str] = {}
        seen_ids: set[str] = set()
        seen_texts: set[str] = set()
        out: list[RecallEntry] = []
        for entry in ranked:
            id_key = f"{entry.kind.value}:{entry.source_id}"
            if id_key in seen_ids:
                continue
            seen_ids.add(id_key)
            if file_state is not None and _mentions_known_file(entry, file_state):
                continue
            sig = _signature_for(entry, sigs)
            if sig in seen_texts:
                continue
            seen_texts.add(sig)
            out.append(entry)
        return out

    @staticmethod
    def _collapse_semantic_over_episodic(
        entries: list[RecallEntry],
    ) -> list[RecallEntry]:
        """When a semantic fact covers the same content as an episodic entry,
        prefer the fact (more stable, higher confidence) and drop the episode."""
        semantic_texts: set[str] = set()
        for e in entries:
            if e.kind is MemoryKind.SEMANTIC:
                semantic_texts.add(_text_signature(e.text))
        if not semantic_texts:
            return entries
        return [
            e for e in entries
            if e.kind is not MemoryKind.EPISODIC
            or _text_signature(e.text) not in semantic_texts
        ]


def _signature_for(entry: RecallEntry, cache: dict[int, str]) -> str:
    """Content fingerprint for ``entry``, memoised per object identity."""
    key = id(entry)
    sig = cache.get(key)
    if sig is None:
        sig = _text_signature(entry.text)
        cache[key] = sig
    return sig


def _text_signature(text: str) -> str:
    """Cheap content fingerprint for near-duplicate collapse."""
    t = (text or "").strip().lower()[:300]
    return t


def _parse_timestamp(raw: str, cache: dict[str, datetime | None]) -> datetime | None:
    """Parse an ISO timestamp once per distinct raw string."""
    cached = cache.get(raw)
    if cached is not None:
        return cached
    try:
        timestamp = datetime.fromisoformat(raw)
    except ValueError:
        cache[raw] = None
        return None
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    cache[raw] = timestamp
    return timestamp


def _apply_boosts(
    entry: RecallEntry,
    *,
    now: datetime,
    ts_cache: dict[str, datetime | None] | None = None,
) -> float:
    base = max(0.0, float(entry.score or 0.0))
    boost = 1.0

    created_at_raw = entry.metadata.get("last_run_at") or entry.metadata.get(
        "created_at"
    )
    if isinstance(created_at_raw, str):
        if ts_cache is None:
            ts_cache = {}
        timestamp = _parse_timestamp(created_at_raw, ts_cache)
        if timestamp is not None:
            age_days = max(0.0, (now - timestamp).total_seconds() / 86_400)
            boost *= 0.5 + 0.5 * math.exp(-age_days / RECENCY_HALF_LIFE_DAYS)

    if entry.kind is MemoryKind.SEMANTIC:
        confidence = float(entry.metadata.get("confidence") or 0.0)
        boost *= 0.5 + 0.5 * max(0.0, min(1.0, confidence))
        boost *= 1.15

    if entry.kind is MemoryKind.PROCEDURAL:
        success_rate = float(entry.metadata.get("success_rate") or 0.0)
        run_count = int(entry.metadata.get("run_count") or 0)
        boost *= 0.4 + 0.6 * success_rate
        if run_count == 0:
            boost *= 0.8
        elif run_count >= 3:
            boost *= 1.1

    if entry.kind is MemoryKind.EPISODIC:
        importance = float(entry.metadata.get("importance") or 0.0)
        boost *= 1.0 + 0.3 * max(0.0, min(1.0, importance))
        recalls = int(entry.metadata.get("recall_count") or 0)
        boost *= 1.0 + min(0.6, 0.04 * recalls)

    return base * boost


def _mentions_known_file(entry: RecallEntry, file_state: FileStateCache) -> bool:
    paths = file_state.paths()
    if not paths:
        return False
    text = entry.text or ""
    return any(path and path in text for path in paths)


__all__ = ["RecallOptions", "RetrievalPipeline"]
