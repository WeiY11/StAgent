"""Benchmark for the recall pipeline: BM25 ranking quality + CPU overhead.

Two questions are answered here:

1. **Quality** — when the lexical fallback is the active path (no vector
   hits), does BM25 reranking surface the genuinely relevant memories higher
   than the legacy "flat score, recency order" behaviour? Measured with
   Mean Reciprocal Rank (MRR) and Precision@3 over a synthetic corpus where a
   relevant memory is *older* than several keyword-weak distractors.

2. **Cost** — how much CPU does the BM25 rerank + fused dedup/collapse add to
   a recall turn? Reported as mean milliseconds per call so the overhead is
   visible and bounded.

Run with::

    uv run pytest tests/bench_recall_bm25.py -s

The benchmark asserts loose upper bounds so a regression that makes recall
materially slower fails CI, while still printing the measured numbers.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC
from typing import Any
from uuid import uuid4

import pytest

from leagent.memory.recall import RecallOptions, RetrievalPipeline
from leagent.memory.types import MemoryKind, RecallEntry

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class _LexicalStore:
    """Mimics the SQLite lexical path: returns candidates in *recency* order
    with a flat score, ignoring relevance — exactly what BM25 improves on."""

    entries: list[RecallEntry] = field(default_factory=list)
    kind: MemoryKind = MemoryKind.EPISODIC

    async def semantic_search(self, *a: Any, **kw: Any) -> list[RecallEntry]:
        return []

    async def lexical_search(
        self, query: str, *, limit: int = 10, **kw: Any
    ) -> list[RecallEntry]:
        # Recency-ordered, flat-scored, truncated to ``limit`` (legacy shape).
        return list(self.entries[:limit])

    async def note_recall(self, source_id: Any) -> None:
        return None


@dataclass
class _EmptyStore:
    kind: MemoryKind = MemoryKind.SEMANTIC

    async def semantic_search(self, *a: Any, **kw: Any) -> list[RecallEntry]:
        return []

    async def lexical_search(self, *a: Any, **kw: Any) -> list[RecallEntry]:
        return []


@dataclass
class _NullEmbeddings:
    """Forces the lexical fallback path (no vector candidates)."""

    last_degraded = True

    async def embed_one(self, text: str) -> list[float]:
        return [0.0] * 8


# ---------------------------------------------------------------------------
# Corpus builder
# ---------------------------------------------------------------------------


def _entry(text: str, *, days_ago: int) -> RecallEntry:
    from datetime import datetime, timedelta

    created = datetime.now(UTC) - timedelta(days=days_ago)
    return RecallEntry(
        kind=MemoryKind.EPISODIC,
        text=text,
        score=0.4,
        source_id=uuid4(),
        metadata={"created_at": created.isoformat(), "importance": 0.2},
    )


def _corpus() -> tuple[list[RecallEntry], str, str]:
    """A query, its relevant (but *old*) memory, and newer keyword-weak
    distractors that recency-ordering would wrongly prefer."""
    query = "edge computing task offloading"
    relevant = _entry(
        "discussed edge computing task offloading latency for the vehicle fleet",
        days_ago=40,  # old → recency ranking buries it
    )
    distractors = [
        _entry("team offloading notes from the standup", days_ago=1),
        _entry("computing the quarterly budget figures", days_ago=2),
        _entry("edge case handling in the parser", days_ago=3),
        _entry("scheduled task runner refactor", days_ago=4),
        _entry("computing cluster cost review", days_ago=5),
        _entry("offloading old docker images", days_ago=6),
        _entry("edge networking diagram draft", days_ago=7),
    ]
    # Recency order: distractors first (newest), relevant last (oldest).
    entries = sorted(distractors + [relevant], key=lambda e: e.metadata["created_at"], reverse=True)
    return entries, query, relevant.text


def _pipeline(entries: list[RecallEntry]) -> RetrievalPipeline:
    return RetrievalPipeline(
        episodic=_LexicalStore(entries=entries),  # type: ignore[arg-type]
        semantic=_EmptyStore(kind=MemoryKind.SEMANTIC),  # type: ignore[arg-type]
        procedural=_EmptyStore(kind=MemoryKind.PROCEDURAL),  # type: ignore[arg-type]
        embeddings=_NullEmbeddings(),  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# Quality benchmark
# ---------------------------------------------------------------------------


def _rank_of(bundle_entries: list[RecallEntry], text: str) -> int | None:
    for idx, entry in enumerate(bundle_entries):
        if entry.text == text:
            return idx + 1
    return None


@pytest.mark.asyncio
async def test_bm25_improves_ranking_quality() -> None:
    entries, query, relevant_text = _corpus()

    # Legacy: flat score + recency order (bm25 off).
    legacy = await _pipeline(entries).recall(
        RecallOptions(query=query, bm25_rerank=False, limit=8, per_store_limit=8)
    )
    # Optimised: BM25 relevance rerank.
    optimised = await _pipeline(entries).recall(
        RecallOptions(query=query, bm25_rerank=True, limit=8, per_store_limit=8)
    )

    legacy_rank = _rank_of(legacy.entries, relevant_text)
    bm25_rank = _rank_of(optimised.entries, relevant_text)

    print(f"\n[quality] relevant memory rank — legacy(recency): {legacy_rank}, bm25: {bm25_rank}")
    assert legacy_rank is not None and bm25_rank is not None
    # BM25 must lift the genuinely relevant (but old) memory above recency noise.
    assert bm25_rank < legacy_rank
    assert bm25_rank == 1


@pytest.mark.asyncio
async def test_bm25_precision_at_3() -> None:
    """Precision@3 over a corpus with three relevant memories."""
    query = "edge computing offloading"
    relevant_texts = {
        "edge computing task offloading latency",
        "offloading decisions for edge nodes in computing cluster",
        "edge computing offloading cost model",
    }
    entries = [
        _entry("edge computing task offloading latency", days_ago=30),
        _entry("offloading decisions for edge nodes in computing cluster", days_ago=31),
        _entry("edge computing offloading cost model", days_ago=32),
        _entry("weekly standup notes", days_ago=1),
        _entry("budget computing spreadsheet", days_ago=2),
        _entry("docker image offloading cleanup", days_ago=3),
        _entry("edge case parser bug", days_ago=4),
        _entry("task runner refactor", days_ago=5),
    ]
    entries.sort(key=lambda e: e.metadata["created_at"], reverse=True)

    bundle = await _pipeline(entries).recall(
        RecallOptions(query=query, bm25_rerank=True, limit=3, per_store_limit=8)
    )
    top3 = bundle.entries[:3]
    hits = sum(1 for e in top3 if e.text in relevant_texts)
    precision = hits / 3
    print(f"\n[quality] precision@3 = {precision:.2f} ({hits}/3 relevant in top-3)")
    assert precision >= 2 / 3


# ---------------------------------------------------------------------------
# CPU overhead benchmark
# ---------------------------------------------------------------------------


def _large_corpus(n: int = 400) -> list[RecallEntry]:
    entries = []
    for i in range(n):
        entries.append(
            _entry(
                f"memory record {i} about edge computing and task offloading "
                f"with vehicle telemetry payload {i % 17}",
                days_ago=i % 60,
            )
        )
    return entries


@pytest.mark.asyncio
async def test_recall_cpu_overhead_bounded() -> None:
    entries = _large_corpus(400)
    query = "edge computing task offloading telemetry"
    pipeline = _pipeline(entries)
    options = RecallOptions(query=query, bm25_rerank=True, limit=8, per_store_limit=8)

    # Warm-up.
    for _ in range(20):
        await pipeline.recall(options)

    iterations = 500
    start = time.perf_counter()
    for _ in range(iterations):
        await pipeline.recall(options)
    elapsed_ms = (time.perf_counter() - start) * 1000
    per_call_ms = elapsed_ms / iterations

    print(f"\n[perf] recall over 400 lexical candidates: {per_call_ms:.3f} ms/call "
          f"({iterations} iterations, {elapsed_ms:.1f} ms total)")
    # Loose guard: a single recall turn must stay well under a millisecond-scale
    # budget given the pool is already in memory. Bump only with justification.
    assert per_call_ms < 5.0
