"""Unit coverage for the quality-profile RAG primitives."""

from __future__ import annotations

from uuid import uuid4

import pytest

from leagent.knowledge.evaluation import RetrievalEvaluationCase, evaluate_retriever
from leagent.knowledge.evidence import EvidenceStatus, build_evidence
from leagent.knowledge.query import DeterministicQueryRewriter
from leagent.knowledge.rerank import LexicalRerankProvider
from leagent.library.retrieval import RetrievalResult, _rrf_fusion_scores


@pytest.mark.asyncio
async def test_query_rewrite_is_bounded_and_keeps_original() -> None:
    result = await DeterministicQueryRewriter().rewrite(
        '查找“向量检索”',
        conversation=[{"role": "user", "content": "知识库"}],
    )
    assert result.original == '查找“向量检索”'
    assert result.variants[0]
    assert len(result.variants) <= 4
    assert result.original in result.variants


@pytest.mark.asyncio
async def test_local_reranker_prefers_phrase_and_term_overlap() -> None:
    ranked = await LexicalRerankProvider().rerank(
        "Milvus 向量索引",
        ["无关文本", "Milvus 向量索引的版本与 ACL 过滤"],
        top_n=2,
    )
    assert ranked[0][0] == 1


def test_rrf_fuses_lexical_and_vector_ranks() -> None:
    lexical = type("Hit", (), {"chunk_id": uuid4()})()
    vector_id = str(uuid4())
    scores = _rrf_fusion_scores([lexical], {vector_id: 0.8})
    assert str(lexical.chunk_id) in scores
    assert vector_id in scores


def test_evidence_gate_marks_empty_retrieval_insufficient() -> None:
    empty = build_evidence([], strict=True)
    assert empty.status is EvidenceStatus.INSUFFICIENT


def test_evidence_gate_builds_stable_citation_ids() -> None:
    result = RetrievalResult(
        file_id=uuid4(),
        chunk_id=uuid4(),
        file_name="guide.md",
        score=0.4,
        snippet="grounded text",
        start_offset=10,
        end_offset=30,
        seq=1,
        text="grounded text",
    )
    bundle = build_evidence([result], strict=True)
    assert bundle.status is EvidenceStatus.SUFFICIENT
    assert bundle.citations[0].citation_id == "K1"


@pytest.mark.asyncio
async def test_retrieval_evaluation_reports_retrieval_gate_and_unproven_answer_gate() -> None:
    first, second = uuid4(), uuid4()

    async def retrieve(query: str, k: int) -> list[dict[str, str]]:
        return [{"chunk_id": str(first)}, {"chunk_id": str(second)}]

    report = await evaluate_retriever(
        [
            RetrievalEvaluationCase(
                query="acl",
                relevant_chunk_ids=frozenset({str(first)}),
            )
        ],
        retrieve,
        k=2,
        recall_target=1.0,
        ndcg_target=1.0,
        p95_ms_target=5000.0,
    )
    assert report.retrieval_gate_passed is True
    assert report.strict_gate_passed is False
    assert report.recall_at_k == 1.0
    assert report.ndcg_at_k == 1.0
