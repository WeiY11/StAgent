"""Regression coverage for retrieval-quality metric calculations."""

from __future__ import annotations

import pytest

from StAgent.knowledge.evaluation import RetrievalEvaluationCase, evaluate_retriever


@pytest.mark.asyncio
async def test_retrieval_evaluation_reports_partial_relevance_at_five() -> None:
    """Recall@5, Precision@5, NDCG@5, and MRR average partial hits correctly."""

    results = {
        "all-relevant": ["a", "noise-1", "b", "noise-2", "noise-3", "ignored"],
        "one-of-two": ["noise-4", "c", "noise-5", "noise-6", "noise-7", "d"],
    }

    async def retrieve(query: str, _k: int) -> list[dict[str, str]]:
        return [{"chunk_id": chunk_id} for chunk_id in results[query]]

    report = await evaluate_retriever(
        [
            RetrievalEvaluationCase(
                case_id="all-relevant",
                query="all-relevant",
                relevant_chunk_ids=frozenset({"a", "b"}),
            ),
            RetrievalEvaluationCase(
                case_id="one-of-two",
                query="one-of-two",
                relevant_chunk_ids=frozenset({"c", "d"}),
            ),
        ],
        retrieve,
        k=5,
        recall_target=0.75,
        ndcg_target=0.65,
        p95_ms_target=5000.0,
    )

    assert report.k == 5
    assert report.recall_at_k == pytest.approx(0.75)
    assert report.precision_at_k == pytest.approx(0.30)
    assert report.ndcg_at_k == pytest.approx(0.6532867982)
    assert report.mrr == pytest.approx(0.75)
    assert report.retrieval_gate_passed is True
    assert report.strict_gate_passed is False


@pytest.mark.asyncio
async def test_retrieval_evaluation_excludes_rank_six_from_recall_at_five() -> None:
    """A relevant chunk after rank five must not count toward Recall@5."""

    async def retrieve(_query: str, _k: int) -> list[dict[str, str]]:
        return [
            {"chunk_id": "noise-1"},
            {"chunk_id": "noise-2"},
            {"chunk_id": "noise-3"},
            {"chunk_id": "noise-4"},
            {"chunk_id": "noise-5"},
            {"chunk_id": "relevant-after-cutoff"},
        ]

    report = await evaluate_retriever(
        [
            RetrievalEvaluationCase(
                case_id="cutoff",
                query="cutoff",
                relevant_chunk_ids=frozenset({"relevant-after-cutoff"}),
            )
        ],
        retrieve,
        k=5,
        recall_target=0.01,
        ndcg_target=0.01,
        p95_ms_target=5000.0,
    )

    assert report.recall_at_k == 0.0
    assert report.precision_at_k == 0.0
    assert report.ndcg_at_k == 0.0
    assert report.mrr == 0.0
    assert report.retrieval_gate_passed is False


@pytest.mark.asyncio
async def test_retrieval_evaluation_reports_precision_at_ten() -> None:
    """Top-ten relevance is measured against ten slots, not returned result count."""

    results = {
        "three-hits": [
            "a",
            "noise-1",
            "noise-2",
            "b",
            "noise-3",
            "noise-4",
            "noise-5",
            "noise-6",
            "noise-7",
            "c",
            "ignored-after-ten",
        ],
        "hit-after-cutoff": [
            "noise-8",
            "noise-9",
            "noise-10",
            "noise-11",
            "noise-12",
            "noise-13",
            "noise-14",
            "noise-15",
            "noise-16",
            "noise-17",
            "d",
        ],
    }

    async def retrieve(query: str, _k: int) -> list[dict[str, str]]:
        return [{"chunk_id": chunk_id} for chunk_id in results[query]]

    report = await evaluate_retriever(
        [
            RetrievalEvaluationCase(
                case_id="three-hits",
                query="three-hits",
                relevant_chunk_ids=frozenset({"a", "b", "c"}),
            ),
            RetrievalEvaluationCase(
                case_id="hit-after-cutoff",
                query="hit-after-cutoff",
                relevant_chunk_ids=frozenset({"d"}),
            ),
        ],
        retrieve,
        k=10,
        recall_target=0.60,
        ndcg_target=0.50,
        p95_ms_target=5000.0,
    )

    assert report.k == 10
    assert report.recall_at_k == pytest.approx(0.50)
    assert report.precision_at_k == pytest.approx(0.15)
    assert report.to_dict()["precision_at_k"] == pytest.approx(0.15)
    assert report.retrieval_gate_passed is False


@pytest.mark.asyncio
async def test_retrieval_evaluation_uses_k_slots_for_short_top_ten_results() -> None:
    """A short result list must not inflate Precision@10."""

    requested_k: list[int] = []

    async def retrieve(_query: str, k: int) -> list[dict[str, str]]:
        requested_k.append(k)
        return [{"chunk_id": "only-relevant-result"}]

    report = await evaluate_retriever(
        [
            RetrievalEvaluationCase(
                case_id="short-result-list",
                query="short-result-list",
                relevant_chunk_ids=frozenset({"only-relevant-result"}),
            )
        ],
        retrieve,
        k=10,
        recall_target=1.0,
        ndcg_target=1.0,
        p95_ms_target=5000.0,
    )

    assert requested_k == [10]
    assert report.recall_at_k == 1.0
    assert report.precision_at_k == pytest.approx(0.10)
    assert report.retrieval_gate_passed is True


@pytest.mark.asyncio
async def test_retrieval_evaluation_uses_k_not_return_count_for_ndcg_ideal_ranking() -> None:
    """nDCG@K keeps missing relevant slots in the ideal ranking denominator."""

    async def retrieve(_query: str, _k: int) -> list[dict[str, str]]:
        return [{"chunk_id": "first-relevant"}]

    report = await evaluate_retriever(
        [
            RetrievalEvaluationCase(
                case_id="short-multi-relevance",
                query="short-multi-relevance",
                relevant_chunk_ids=frozenset({"first-relevant", "missing-relevant"}),
            )
        ],
        retrieve,
        k=10,
        recall_target=1.0,
        ndcg_target=0.9,
        p95_ms_target=5000.0,
    )

    assert report.recall_at_k == pytest.approx(0.5)
    assert report.precision_at_k == pytest.approx(0.1)
    assert report.ndcg_at_k == pytest.approx(1 / (1 + 1 / 1.5849625007))
    assert report.retrieval_gate_passed is False


@pytest.mark.asyncio
async def test_retrieval_evaluation_does_not_reward_duplicate_relevant_ids() -> None:
    """A duplicate relevant chunk occupies a rank but cannot add a second gain."""

    async def retrieve(_query: str, _k: int) -> list[dict[str, str]]:
        return [
            {"chunk_id": "a"},
            {"chunk_id": "a"},
            {"chunk_id": "b"},
        ]

    report = await evaluate_retriever(
        [
            RetrievalEvaluationCase(
                case_id="duplicate-result",
                query="duplicate-result",
                relevant_chunk_ids=frozenset({"a", "b"}),
            )
        ],
        retrieve,
        k=3,
        recall_target=1.0,
        ndcg_target=0.90,
        p95_ms_target=5000.0,
    )

    assert report.recall_at_k == 1.0
    assert report.precision_at_k == pytest.approx(2 / 3)
    assert report.ndcg_at_k == pytest.approx(0.9197207891)
    assert report.mrr == 1.0
