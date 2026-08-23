"""Offline retrieval evaluation and quality-gate calculations.

The evaluator is intentionally independent of a particular vector database or
LLM.  It accepts an async retrieval callable, which makes it usable from the
REST management endpoint, CI fixtures, and a future replay job without
duplicating scoring code.

Retrieval metrics are evidence about the retriever only.  Citation accuracy
and hallucination rate are accepted as optional answer-evaluation inputs; when
they are absent the strict answer gate remains *unproven* rather than silently
passing.
"""

from __future__ import annotations

import math
import time
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from StAgent.knowledge.evidence import AnswerGroundingResult


@dataclass(frozen=True)
class RetrievalEvaluationCase:
    """One query and its judged relevant chunk identifiers."""

    query: str
    relevant_chunk_ids: frozenset[str]
    case_id: str = ""

    @classmethod
    def from_mapping(cls, value: Any, *, index: int = 0) -> RetrievalEvaluationCase:
        if not isinstance(value, dict):
            raise ValueError("evaluation case must be an object")
        query = str(value.get("query") or "").strip()
        if not query:
            raise ValueError("evaluation case query is required")
        raw_ids = value.get("relevant_chunk_ids") or value.get("relevant_ids") or []
        if not isinstance(raw_ids, (list, tuple, set, frozenset)):
            raise ValueError("relevant_chunk_ids must be a list")
        ids = frozenset(str(item).strip() for item in raw_ids if str(item).strip())
        if not ids:
            raise ValueError("evaluation case needs at least one relevant_chunk_id")
        return cls(query=query, relevant_chunk_ids=ids, case_id=str(value.get("id") or index))


@dataclass(frozen=True)
class RetrievalEvaluationReport:
    """Aggregate retrieval and strict-answer gate results."""

    cases: int
    k: int
    recall_at_k: float
    ndcg_at_k: float
    mrr: float
    retrieval_p95_ms: float
    retrieval_gate_passed: bool
    strict_gate_passed: bool
    citation_accuracy: float | None
    hallucination_rate: float | None
    gate: dict[str, float | None]

    def to_dict(self) -> dict[str, Any]:
        return {
            "cases": self.cases,
            "k": self.k,
            "recall_at_k": self.recall_at_k,
            "ndcg_at_k": self.ndcg_at_k,
            "mrr": self.mrr,
            "retrieval_p95_ms": self.retrieval_p95_ms,
            "retrieval_gate_passed": self.retrieval_gate_passed,
            "strict_gate_passed": self.strict_gate_passed,
            "citation_accuracy": self.citation_accuracy,
            "hallucination_rate": self.hallucination_rate,
            "gate": self.gate,
        }


@dataclass(frozen=True)
class CitationValidationReport:
    """Aggregate automatic citation-token validation results.

    ``hallucination_rate`` intentionally remains ``None``. Citation identity
    checks prove neither entailment nor completeness of an answer.
    """

    answers: int
    strict_acceptance_rate: float | None
    citation_reference_accuracy: float | None
    hallucination_rate: None = None

    def to_dict(self) -> dict[str, float | int | None]:
        return {
            "answers": self.answers,
            "strict_acceptance_rate": self.strict_acceptance_rate,
            "citation_reference_accuracy": self.citation_reference_accuracy,
            "hallucination_rate": self.hallucination_rate,
        }


RetrievalCallable = Callable[[str, int], Awaitable[Sequence[Any]]]


def _result_id(value: Any) -> str | None:
    """Extract a stable chunk id from a result object or mapping."""
    if isinstance(value, dict):
        raw = value.get("chunk_id") or value.get("id")
    else:
        raw = getattr(value, "chunk_id", None) or getattr(value, "id", None)
    text = str(raw).strip() if raw is not None else ""
    return text or None


def _recall(retrieved: Sequence[str], relevant: frozenset[str]) -> float:
    return len(set(retrieved) & relevant) / len(relevant)


def _ndcg(retrieved: Sequence[str], relevant: frozenset[str]) -> float:
    dcg = sum(
        1.0 / math.log2(rank + 2)
        for rank, item in enumerate(retrieved)
        if item in relevant
    )
    ideal_len = min(len(relevant), len(retrieved))
    if ideal_len <= 0:
        return 0.0
    idcg = sum(1.0 / math.log2(rank + 2) for rank in range(ideal_len))
    return dcg / idcg if idcg else 0.0


def _mrr(retrieved: Sequence[str], relevant: frozenset[str]) -> float:
    for rank, item in enumerate(retrieved, start=1):
        if item in relevant:
            return 1.0 / rank
    return 0.0


def _p95(values: Iterable[float]) -> float:
    ordered = sorted(max(0.0, value) for value in values)
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, max(0, math.ceil(len(ordered) * 0.95) - 1))
    return ordered[index]


async def evaluate_retriever(
    cases: Sequence[RetrievalEvaluationCase],
    retrieve: RetrievalCallable,
    *,
    k: int = 10,
    recall_target: float = 0.90,
    ndcg_target: float = 0.80,
    p95_ms_target: float = 5000.0,
    citation_accuracy: float | None = None,
    hallucination_rate: float | None = None,
    citation_accuracy_target: float = 0.95,
    hallucination_rate_max: float = 0.02,
) -> RetrievalEvaluationReport:
    """Run cases and compute a deterministic quality-gate report."""
    if not cases:
        raise ValueError("at least one evaluation case is required")
    k = max(1, min(int(k), 100))
    recalls: list[float] = []
    ndcgs: list[float] = []
    mrrs: list[float] = []
    latencies: list[float] = []
    for case in cases:
        started = time.perf_counter()
        raw_results = await retrieve(case.query, k)
        latencies.append((time.perf_counter() - started) * 1000.0)
        retrieved = [
            result_id
            for result in list(raw_results)[:k]
            if (result_id := _result_id(result)) is not None
        ]
        recalls.append(_recall(retrieved, case.relevant_chunk_ids))
        ndcgs.append(_ndcg(retrieved, case.relevant_chunk_ids))
        mrrs.append(_mrr(retrieved, case.relevant_chunk_ids))

    recall_at_k = sum(recalls) / len(recalls)
    ndcg_at_k = sum(ndcgs) / len(ndcgs)
    mrr = sum(mrrs) / len(mrrs)
    retrieval_p95_ms = _p95(latencies)
    retrieval_gate_passed = (
        recall_at_k >= recall_target
        and ndcg_at_k >= ndcg_target
        and retrieval_p95_ms <= p95_ms_target
    )
    answer_metrics_present = citation_accuracy is not None and hallucination_rate is not None
    strict_gate_passed = retrieval_gate_passed and answer_metrics_present and (
        citation_accuracy >= citation_accuracy_target
        and hallucination_rate <= hallucination_rate_max
    )
    return RetrievalEvaluationReport(
        cases=len(cases),
        k=k,
        recall_at_k=recall_at_k,
        ndcg_at_k=ndcg_at_k,
        mrr=mrr,
        retrieval_p95_ms=retrieval_p95_ms,
        retrieval_gate_passed=retrieval_gate_passed,
        strict_gate_passed=strict_gate_passed,
        citation_accuracy=citation_accuracy,
        hallucination_rate=hallucination_rate,
        gate={
            "recall_at_k_target": recall_target,
            "ndcg_at_k_target": ndcg_target,
            "retrieval_p95_ms_target": p95_ms_target,
            "citation_accuracy_target": citation_accuracy_target,
            "hallucination_rate_max": hallucination_rate_max,
        },
    )


def evaluate_citation_validation(
    results: Sequence[AnswerGroundingResult],
) -> CitationValidationReport:
    """Summarize reference validity without claiming semantic grounding."""
    answers = len(results)
    references = sum(len(result.referenced_citation_ids) for result in results)
    invalid = sum(len(result.invalid_citation_ids) for result in results)
    accepted = sum(1 for result in results if result.accepted)
    return CitationValidationReport(
        answers=answers,
        strict_acceptance_rate=(accepted / answers) if answers else None,
        citation_reference_accuracy=(references - invalid) / references if references else None,
    )


__all__ = [
    "CitationValidationReport",
    "RetrievalEvaluationCase",
    "RetrievalEvaluationReport",
    "evaluate_retriever",
    "evaluate_citation_validation",
]
