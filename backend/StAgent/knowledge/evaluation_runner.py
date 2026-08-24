"""Run an auditable, lexical-only public retrieval evaluation in isolation.

The runner deliberately creates a fresh SQLite catalog and managed blob root
under a caller-selected run directory. It does not start application services,
load an embedding model, use an external reranker, or contact OpenSearch.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import text

from StAgent.config.settings import DatabaseSettings, FilesSettings, Settings, get_settings
from StAgent.db.repositories.knowledge_index import INDEX_VERSION
from StAgent.db.service import DatabaseService
from StAgent.file.service import FileService
from StAgent.file.storage import LocalStorageBackend
from StAgent.knowledge.acl import ensure_personal_knowledge_space
from StAgent.knowledge.evaluation import RetrievalEvaluationCase, evaluate_retriever
from StAgent.knowledge.evaluation_dataset import PublicRetrievalDataset, build_evaluation_cases
from StAgent.knowledge.evaluation_import import ImportedEvaluationPassage, import_public_retrieval_dataset
from StAgent.knowledge.rerank import LexicalRerankProvider
from StAgent.knowledge.vector_index import get_knowledge_vector_index, set_knowledge_vector_index
from StAgent.library.retrieval import (
    MAX_RERANK_CANDIDATES,
    RERANK_CANDIDATE_MULTIPLIER,
    KnowledgeRetrievalPipeline,
    SearchOptions,
)
from StAgent.services.auth.service import LOCAL_USER_ID

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence
    from pathlib import Path


_EVALUATION_CHUNKING_STRATEGY = "one-passage-one-chunk-v1"


class _DisabledOpenSearch:
    """Explicit no-network OpenSearch adapter for isolated measurements."""

    async def search(
        self,
        _query: str,
        *,
        limit: int = 0,
        index_version: str = "rag-v1",
    ) -> list[Any]:
        del limit, index_version
        return []


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    """Persist a fresh UTF-8 JSON result without overwriting prior evidence."""

    with path.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")


def _write_jsonl(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    """Persist ordered per-case evidence without overwriting prior evidence."""

    with path.open("x", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
            handle.write("\n")


def _file_sha256(path: Path) -> str:
    """Return the SHA-256 of a persisted audit artifact."""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _nearest_rank(values: Sequence[float], percentile: float) -> float:
    """Return a deterministic nearest-rank percentile in milliseconds."""

    if not values:
        return 0.0
    ordered = sorted(max(0.0, value) for value in values)
    index = min(len(ordered) - 1, max(0, math.ceil(len(ordered) * percentile) - 1))
    return ordered[index]


@contextmanager
def _lexical_only_runtime() -> Iterator[None]:
    """Temporarily disable optional rewrite/vector state in this process."""

    setting_name = "StAgent_KNOWLEDGE_QUERY_REWRITE_ENABLED"
    prior_setting = os.environ.get(setting_name)
    prior_vector_index = get_knowledge_vector_index()
    os.environ[setting_name] = "false"
    get_settings.cache_clear()
    set_knowledge_vector_index(None)
    try:
        yield
    finally:
        if prior_setting is None:
            os.environ.pop(setting_name, None)
        else:
            os.environ[setting_name] = prior_setting
        get_settings.cache_clear()
        set_knowledge_vector_index(prior_vector_index)


async def _sqlite_runtime_info(db: DatabaseService) -> dict[str, Any]:
    """Capture SQLite and trigram-trigger state that affects CJK retrieval."""

    async with db.session() as session:
        sqlite_version = (await session.execute(text("SELECT sqlite_version()"))).scalar_one()
        trigger_rows = (
            await session.execute(
                text("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE :prefix ORDER BY name"),
                {"prefix": "document_chunks_trigram_%"},
            )
        ).all()
    return {
        "sqlite_version": str(sqlite_version),
        "trigram_triggers": [str(row[0]) for row in trigger_rows],
    }


def _mapping_validation(
    dataset: PublicRetrievalDataset,
    mappings: Sequence[ImportedEvaluationPassage],
) -> dict[str, int]:
    """Check the complete one-passage-to-one-chunk precondition in memory."""

    source_ids = [mapping.source_id for mapping in mappings]
    chunk_ids = [str(mapping.chunk_id) for mapping in mappings]
    expected_sources = [record.source_id for record in dataset.corpus]
    if source_ids != expected_sources:
        raise RuntimeError("evaluation import source ordering or coverage does not match the corpus")
    duplicate_chunk_ids = len(chunk_ids) - len(set(chunk_ids))
    if duplicate_chunk_ids:
        raise RuntimeError("evaluation import mapped multiple passages to one chunk")
    return {
        "corpus_passages": len(dataset.corpus),
        "files": len(mappings),
        "chunks": len(chunk_ids),
        "duplicate_chunk_ids": duplicate_chunk_ids,
    }


def _case_record(
    case: RetrievalEvaluationCase,
    result_ids: Sequence[str],
    chunk_to_source: Mapping[str, str],
    *,
    latency_ms: float,
    error: str | None,
) -> dict[str, Any]:
    """Build one audit row with rank, returned source IDs, and latency."""

    first_relevant_rank: int | None = None
    seen: set[str] = set()
    unique_result_ids: list[str] = []
    for rank, chunk_id in enumerate(result_ids, start=1):
        if chunk_id in seen:
            continue
        seen.add(chunk_id)
        unique_result_ids.append(chunk_id)
        if first_relevant_rank is None and chunk_id in case.relevant_chunk_ids:
            first_relevant_rank = rank
    return {
        "case_id": case.case_id,
        "relevant_chunk_ids": sorted(case.relevant_chunk_ids),
        "relevant_source_ids": sorted(chunk_to_source.get(chunk_id, "") for chunk_id in case.relevant_chunk_ids),
        "result_chunk_ids": unique_result_ids,
        "result_source_ids": [chunk_to_source.get(chunk_id) for chunk_id in unique_result_ids],
        "first_relevant_rank": first_relevant_rank,
        "unique_result_count": len(unique_result_ids),
        "latency_ms": latency_ms,
        "error": error,
    }


async def _evaluate_configuration(
    *,
    name: str,
    pipeline: KnowledgeRetrievalPipeline,
    cases: Sequence[RetrievalEvaluationCase],
    user_id: Any,
    knowledge_space_id: Any,
    rerank: bool,
    chunk_to_source: Mapping[str, str],
    run_dir: Path,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Measure one local retrieval configuration at K=5 and K=10."""

    async def retrieve(query: str, k: int) -> list[Any]:
        return await pipeline.search(
            SearchOptions(
                query=query,
                user_id=user_id,
                space_id=knowledge_space_id,
                library_scope="knowledge",
                limit=k,
                query_rewrite=False,
                rerank=rerank,
            )
        )

    report_at_5 = await evaluate_retriever(cases, retrieve, k=5)
    report_at_10 = await evaluate_retriever(cases, retrieve, k=10)

    case_records: list[dict[str, Any]] = []
    case_latencies: list[float] = []
    for case in cases:
        started = time.perf_counter()
        try:
            results = await retrieve(case.query, 10)
            error: str | None = None
        except Exception as exc:  # noqa: BLE001 - retain the failed-case evidence
            results = []
            error = f"{type(exc).__name__}: {exc}"
        latency_ms = (time.perf_counter() - started) * 1000.0
        case_latencies.append(latency_ms)
        case_records.append(
            _case_record(
                case,
                [str(result.chunk_id) for result in results],
                chunk_to_source,
                latency_ms=latency_ms,
                error=error,
            )
        )

    records_file = f"{name}_cases.jsonl"
    _write_jsonl(run_dir / records_file, case_records)
    errors = sum(record["error"] is not None for record in case_records)
    empty_results = sum(record["unique_result_count"] == 0 for record in case_records)
    hit_at_1 = sum(bool(record["first_relevant_rank"] == 1) for record in case_records) / len(case_records)
    return (
        {
            "rerank": rerank,
            "reranker": "LexicalRerankProvider" if rerank else "disabled",
            "query_rewrite": False,
            "cache": "disabled",
            "candidate_pool": {
                "multiplier_when_reranking": RERANK_CANDIDATE_MULTIPLIER,
                "maximum": MAX_RERANK_CANDIDATES,
            },
            "metrics": {
                "at_5": report_at_5.to_dict(),
                "at_10": report_at_10.to_dict(),
                "top1_retrieval_hit_rate": hit_at_1,
            },
            "query_count": len(case_records),
            "empty_results": empty_results,
            "errors": errors,
            "latency_ms_top10": {
                "p50": _nearest_rank(case_latencies, 0.50),
                "p95": _nearest_rank(case_latencies, 0.95),
                "p99": _nearest_rank(case_latencies, 0.99),
                "min": min(case_latencies, default=0.0),
                "max": max(case_latencies, default=0.0),
            },
            "case_records_file": records_file,
        },
        case_records,
    )


def _paired_delta(
    baseline: Sequence[Mapping[str, Any]],
    candidate: Sequence[Mapping[str, Any]],
) -> dict[str, float | int]:
    """Summarize paired top-10 rank changes without claiming statistical significance."""

    baseline_by_case = {str(record["case_id"]): record for record in baseline}
    candidate_by_case = {str(record["case_id"]): record for record in candidate}
    if set(baseline_by_case) != set(candidate_by_case):
        raise RuntimeError("configuration case coverage differs")
    wins = 0
    losses = 0
    ties = 0
    recall_delta = 0.0
    mrr_delta = 0.0
    for case_id, before in baseline_by_case.items():
        after = candidate_by_case[case_id]
        before_rank = before["first_relevant_rank"]
        after_rank = after["first_relevant_rank"]
        before_score = 0.0 if before_rank is None else 1.0 / int(before_rank)
        after_score = 0.0 if after_rank is None else 1.0 / int(after_rank)
        recall_delta += float(after_rank is not None) - float(before_rank is not None)
        mrr_delta += after_score - before_score
        before_order = math.inf if before_rank is None else int(before_rank)
        after_order = math.inf if after_rank is None else int(after_rank)
        if after_order < before_order:
            wins += 1
        elif after_order > before_order:
            losses += 1
        else:
            ties += 1
    count = len(baseline_by_case)
    return {
        "cases": count,
        "wins": wins,
        "losses": losses,
        "ties": ties,
        "delta_recall_at_10": recall_delta / count if count else 0.0,
        "delta_mrr_at_10": mrr_delta / count if count else 0.0,
    }


async def run_isolated_public_retrieval_evaluation(
    dataset: PublicRetrievalDataset,
    run_dir: Path,
    *,
    source_manifest: Mapping[str, Any] | None = None,
    source_manifest_sha256: str | None = None,
) -> dict[str, Any]:
    """Import and measure a public retrieval set in a fresh local SQLite run.

    ``run_dir`` must not already exist. The function keeps a failed run on
    disk with ``failure.json`` for inspection instead of overwriting evidence.
    """

    run_dir = run_dir.resolve()
    if run_dir.exists():
        raise FileExistsError(f"refusing to overwrite evaluation run directory: {run_dir}")
    run_dir.mkdir(parents=True)
    db_path = run_dir / "evaluation.sqlite3"
    blobs_dir = run_dir / "blobs"
    started_at = datetime.now(UTC).isoformat(timespec="seconds")
    settings = Settings(
        database=DatabaseSettings(sqlite_path=str(db_path)),
        files=FilesSettings(upload_dir=str(blobs_dir)),
    )
    db = DatabaseService(settings)
    try:
        with _lexical_only_runtime():
            await db.create_tables()
            knowledge_space_id = await ensure_personal_knowledge_space(db, LOCAL_USER_ID)
            file_service = FileService(
                default_backend=LocalStorageBackend(blobs_dir),
                database=db,
            )
            mappings = await import_public_retrieval_dataset(
                db,
                file_service,
                dataset,
                user_id=LOCAL_USER_ID,
                knowledge_space_id=knowledge_space_id,
            )
            validation: dict[str, Any] = _mapping_validation(dataset, mappings)
            mapping_path = run_dir / "chunk_mapping.jsonl"
            _write_jsonl(mapping_path, [mapping.to_dict() for mapping in mappings])
            validation.update(
                {
                    "mapping_sha256": _file_sha256(mapping_path),
                    "chunking_strategy": _EVALUATION_CHUNKING_STRATEGY,
                    "index_version": INDEX_VERSION,
                }
            )
            cases = build_evaluation_cases(
                dataset,
                source_to_chunk_ids={mapping.source_id: str(mapping.chunk_id) for mapping in mappings},
            )
            if not cases:
                raise RuntimeError("prepared evaluation dataset produced no judged query cases")
            chunk_to_source = {str(mapping.chunk_id): mapping.source_id for mapping in mappings}
            pipeline = KnowledgeRetrievalPipeline(
                db,
                reranker=LexicalRerankProvider(),
                opensearch=_DisabledOpenSearch(),
            )
            baseline, baseline_cases = await _evaluate_configuration(
                name="lexical_baseline",
                pipeline=pipeline,
                cases=cases,
                user_id=LOCAL_USER_ID,
                knowledge_space_id=knowledge_space_id,
                rerank=False,
                chunk_to_source=chunk_to_source,
                run_dir=run_dir,
            )
            reranked, reranked_cases = await _evaluate_configuration(
                name="lexical_rerank",
                pipeline=pipeline,
                cases=cases,
                user_id=LOCAL_USER_ID,
                knowledge_space_id=knowledge_space_id,
                rerank=True,
                chunk_to_source=chunk_to_source,
                run_dir=run_dir,
            )
            runtime_info = await _sqlite_runtime_info(db)
            result: dict[str, Any] = {
                "schema_version": 1,
                "started_at": started_at,
                "completed_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "dataset": {
                    "dataset_id": dataset.dataset_id,
                    "revision": dataset.revision,
                    "corpus_passages": len(dataset.corpus),
                    "queries": len(dataset.queries),
                    "qrels": len(dataset.qrels),
                    "source_manifest": dict(source_manifest) if source_manifest is not None else None,
                    "source_manifest_sha256": source_manifest_sha256,
                },
                "run_paths": {
                    "run_dir": str(run_dir),
                    "database": str(db_path),
                    "blobs": str(blobs_dir),
                },
                "isolation": {
                    "user_id": str(LOCAL_USER_ID),
                    "knowledge_space_id": str(knowledge_space_id),
                    "vector_embedding": "disabled",
                    "query_rewrite": "disabled",
                    "opensearch": "disabled",
                    "external_reranker": "disabled",
                },
                "runtime": runtime_info,
                "mapping_validation": validation,
                "answer_accuracy": None,
                "answer_accuracy_note": "Not evaluated; retrieval qrels cannot establish answer correctness.",
                "configurations": {
                    "lexical_baseline": baseline,
                    "lexical_rerank": reranked,
                },
                "paired_comparison": {
                    "lexical_rerank_vs_baseline": _paired_delta(baseline_cases, reranked_cases),
                    "interpretation": "single local paired run; no confidence interval or release gate calibration",
                },
            }
            _write_json(run_dir / "results.json", result)
            return result
    except Exception as exc:
        _write_json(
            run_dir / "failure.json",
            {
                "started_at": started_at,
                "failed_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "error": f"{type(exc).__name__}: {exc}",
            },
        )
        raise
    finally:
        await db.dispose()


__all__ = ["run_isolated_public_retrieval_evaluation"]
