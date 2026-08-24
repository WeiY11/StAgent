"""Validated conversion of public retrieval datasets into local evaluation cases.

Public benchmarks commonly judge relevance at document or passage level, while
``evaluate_retriever`` operates on LeAgent chunk IDs.  This module makes that
boundary explicit: a source passage must map to exactly one local chunk before
its qrels can become ``RetrievalEvaluationCase`` inputs.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from StAgent.knowledge.evaluation import RetrievalEvaluationCase


@dataclass(frozen=True)
class PublicRetrievalRecord:
    """A public corpus passage or query with its upstream-stable identifier."""

    source_id: str
    text: str


@dataclass(frozen=True)
class PublicRetrievalQrel:
    """One binary upstream relevance judgment."""

    query_source_id: str
    corpus_source_id: str


@dataclass(frozen=True)
class PublicRetrievalDataset:
    """Validated public corpus, queries, and document-level qrels."""

    dataset_id: str
    revision: str
    corpus: tuple[PublicRetrievalRecord, ...]
    queries: tuple[PublicRetrievalRecord, ...]
    qrels: tuple[PublicRetrievalQrel, ...]


def _required_text(row: Mapping[str, Any], *, field: str, row_label: str) -> str:
    """Return a non-empty scalar field from one public dataset row."""
    value = str(row.get(field) or "").strip()
    if not value:
        raise ValueError(f"{row_label} requires non-empty {field}")
    return value


def _parse_records(
    rows: Sequence[Mapping[str, Any]],
    *,
    row_kind: str,
) -> tuple[PublicRetrievalRecord, ...]:
    """Validate public ``_id``/``text`` rows and preserve their source order."""
    records: list[PublicRetrievalRecord] = []
    source_ids: set[str] = set()
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise ValueError(f"{row_kind} row {index} must be an object")
        row_label = f"{row_kind} row {index}"
        source_id = _required_text(row, field="_id", row_label=row_label)
        if source_id in source_ids:
            raise ValueError(f"duplicate {row_kind} id '{source_id}'")
        source_ids.add(source_id)
        records.append(
            PublicRetrievalRecord(
                source_id=source_id,
                text=_required_text(row, field="text", row_label=row_label),
            )
        )
    if not records:
        raise ValueError(f"at least one {row_kind} row is required")
    return tuple(records)


def parse_public_retrieval_dataset(
    *,
    dataset_id: str,
    revision: str,
    corpus_rows: Sequence[Mapping[str, Any]],
    query_rows: Sequence[Mapping[str, Any]],
    qrel_rows: Sequence[Mapping[str, Any]],
) -> PublicRetrievalDataset:
    """Validate generic ``_id``/``text`` rows plus ``query-id``/``corpus-id`` qrels."""
    normalized_dataset_id = str(dataset_id).strip()
    normalized_revision = str(revision).strip()
    if not normalized_dataset_id:
        raise ValueError("dataset_id is required")
    if not normalized_revision:
        raise ValueError("revision is required")

    corpus = _parse_records(corpus_rows, row_kind="corpus")
    queries = _parse_records(query_rows, row_kind="query")
    corpus_ids = {record.source_id for record in corpus}
    query_ids = {record.source_id for record in queries}
    qrels: list[PublicRetrievalQrel] = []
    seen_pairs: set[tuple[str, str]] = set()
    for index, row in enumerate(qrel_rows):
        if not isinstance(row, Mapping):
            raise ValueError(f"qrel {index} must be an object")
        row_label = f"qrel {index}"
        query_source_id = _required_text(row, field="query-id", row_label=row_label)
        corpus_source_id = _required_text(row, field="corpus-id", row_label=row_label)
        if query_source_id not in query_ids:
            raise ValueError(f"qrel {index} references unknown query id '{query_source_id}'")
        if corpus_source_id not in corpus_ids:
            raise ValueError(f"qrel {index} references unknown corpus id '{corpus_source_id}'")
        pair = (query_source_id, corpus_source_id)
        if pair in seen_pairs:
            raise ValueError(f"duplicate qrel for query '{query_source_id}' and corpus '{corpus_source_id}'")
        seen_pairs.add(pair)
        qrels.append(
            PublicRetrievalQrel(
                query_source_id=query_source_id,
                corpus_source_id=corpus_source_id,
            )
        )
    if not qrels:
        raise ValueError("at least one qrel row is required")
    return PublicRetrievalDataset(
        dataset_id=normalized_dataset_id,
        revision=normalized_revision,
        corpus=corpus,
        queries=queries,
        qrels=tuple(qrels),
    )


def _normalize_chunk_ids(source_id: str, raw_chunk_ids: Sequence[str] | str | None) -> str:
    """Require one local chunk ID for one document-level source passage."""
    if isinstance(raw_chunk_ids, str):
        chunk_ids = [raw_chunk_ids.strip()] if raw_chunk_ids.strip() else []
    elif isinstance(raw_chunk_ids, Sequence):
        chunk_ids = [str(chunk_id).strip() for chunk_id in raw_chunk_ids if str(chunk_id).strip()]
    else:
        chunk_ids = []
    if len(chunk_ids) != 1:
        raise ValueError(f"{source_id} must map to exactly one chunk")
    return chunk_ids[0]


def build_evaluation_cases(
    dataset: PublicRetrievalDataset,
    *,
    source_to_chunk_ids: Mapping[str, Sequence[str] | str],
) -> list[RetrievalEvaluationCase]:
    """Convert source qrels only when every corpus passage has one local chunk.

    Requiring a complete 1:1 mapping prevents two silent benchmark errors:
    evaluating a partial corpus and treating every chunk of a source document as
    independently relevant.
    """
    corpus_ids = {record.source_id for record in dataset.corpus}
    unexpected_source_ids = set(source_to_chunk_ids) - corpus_ids
    if unexpected_source_ids:
        unexpected = sorted(unexpected_source_ids)[0]
        raise ValueError(f"chunk map references unknown corpus id '{unexpected}'")

    source_to_chunk_id: dict[str, str] = {}
    seen_chunk_ids: set[str] = set()
    for record in dataset.corpus:
        chunk_id = _normalize_chunk_ids(record.source_id, source_to_chunk_ids.get(record.source_id))
        if chunk_id in seen_chunk_ids:
            raise ValueError(f"chunk id '{chunk_id}' maps to multiple corpus passages")
        seen_chunk_ids.add(chunk_id)
        source_to_chunk_id[record.source_id] = chunk_id

    relevant_chunks_by_query: defaultdict[str, set[str]] = defaultdict(set)
    for qrel in dataset.qrels:
        relevant_chunks_by_query[qrel.query_source_id].add(source_to_chunk_id[qrel.corpus_source_id])
    return [
        RetrievalEvaluationCase(
            case_id=query.source_id,
            query=query.text,
            relevant_chunk_ids=frozenset(relevant_chunks_by_query[query.source_id]),
        )
        for query in dataset.queries
        if query.source_id in relevant_chunks_by_query
    ]


__all__ = [
    "PublicRetrievalDataset",
    "PublicRetrievalQrel",
    "PublicRetrievalRecord",
    "build_evaluation_cases",
    "parse_public_retrieval_dataset",
]
