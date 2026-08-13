"""Answer-level citation validation for knowledge-context responses."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from leagent.context.sources.base import ResolveContext
from leagent.context.sources.knowledge_context import KnowledgeContextSource
from leagent.knowledge.evaluation import evaluate_citation_validation
from leagent.knowledge.evidence import (
    Citation,
    EvidenceBundle,
    EvidenceStatus,
    render_citation_legend,
    validate_answer_citations,
)


def _evidence() -> EvidenceBundle:
    return EvidenceBundle(
        status=EvidenceStatus.SUFFICIENT,
        citations=(
            Citation(
                citation_id="K1",
                file_id=uuid4(),
                file_name="guide.md",
                chunk_id=uuid4(),
                start_offset=0,
                end_offset=10,
                score=0.9,
            ),
            Citation(
                citation_id="K2",
                file_id=uuid4(),
                file_name="policy.md",
                chunk_id=uuid4(),
                start_offset=20,
                end_offset=35,
                score=0.8,
            ),
        ),
    )


def test_strict_grounding_rejects_answer_without_valid_citation() -> None:
    result = validate_answer_citations("unsupported answer", _evidence(), strict=True)

    assert result.accepted is False
    assert result.reason == "missing_citation"
    assert result.referenced_citation_ids == ()


def test_grounding_accepts_valid_citations_and_deduplicates_references() -> None:
    result = validate_answer_citations("The policy applies. [K1] [K1] [K2]", _evidence(), strict=True)

    assert result.accepted is True
    assert result.referenced_citation_ids == ("K1", "K2")
    assert result.invalid_citation_ids == ()


def test_grounding_rejects_unknown_citations_even_when_not_strict() -> None:
    result = validate_answer_citations("Invented source [K99]", _evidence(), strict=False)

    assert result.accepted is False
    assert result.invalid_citation_ids == ("K99",)
    assert result.reason == "invalid_citation"


def test_citation_legend_exposes_only_referenced_sources() -> None:
    legend = render_citation_legend(_evidence(), ("K2",))

    assert "[K2]" in legend
    assert "[K1]" not in legend


def test_strict_grounding_rejects_degraded_evidence() -> None:
    evidence = EvidenceBundle(EvidenceStatus.DEGRADED, _evidence().citations)

    result = validate_answer_citations("Answer [K1]", evidence, strict=True)

    assert result.accepted is False
    assert result.reason == "evidence_not_sufficient"


def test_citation_validation_report_is_not_a_hallucination_claim() -> None:
    accepted = validate_answer_citations("Answer [K1]", _evidence(), strict=True)
    rejected = validate_answer_citations("Answer [K99]", _evidence(), strict=True)

    report = evaluate_citation_validation([accepted, rejected])

    assert report.answers == 2
    assert report.citation_reference_accuracy == 0.5
    assert report.strict_acceptance_rate == 0.5
    assert report.hallucination_rate is None


@pytest.mark.asyncio
async def test_knowledge_context_instructs_the_model_to_use_only_valid_citations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Pipeline:
        async def search(self, _options: object) -> list[SimpleNamespace]:
            return [
                SimpleNamespace(
                    snippet="The source-backed excerpt.",
                    text="The source-backed excerpt.",
                    file_name="guide.md",
                    file_id=uuid4(),
                    chunk_id=uuid4(),
                    score=0.9,
                    start_offset=0,
                    end_offset=26,
                )
            ]

    monkeypatch.setattr(
        "leagent.library.retrieval.get_knowledge_retrieval_pipeline",
        lambda: Pipeline(),
    )

    block = await KnowledgeContextSource().resolve(
        ResolveContext(query="based on knowledge", user_id=uuid4())
    )

    assert block is not None
    assert "include at least one valid [K#] reference" in block.body
    evidence = block.metadata["knowledge_evidence"]
    assert evidence.status is EvidenceStatus.SUFFICIENT
    assert evidence.citations[0].citation_id == "K1"


@pytest.mark.asyncio
async def test_knowledge_context_returns_fail_closed_block_when_search_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Pipeline:
        async def search(self, _options: object) -> list[SimpleNamespace]:
            return []

    monkeypatch.setattr(
        "leagent.library.retrieval.get_knowledge_retrieval_pipeline",
        lambda: Pipeline(),
    )

    block = await KnowledgeContextSource().resolve(
        ResolveContext(query="based on knowledge", user_id=uuid4())
    )

    assert block is not None
    assert "No verifiable knowledge-base evidence" in block.body
    evidence = block.metadata["knowledge_evidence"]
    assert evidence.status is EvidenceStatus.INSUFFICIENT
    assert evidence.citations == ()


@pytest.mark.asyncio
async def test_generic_document_authoring_request_does_not_trigger_empty_kb_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Pipeline:
        async def search(self, _options: object) -> list[SimpleNamespace]:
            return []

    monkeypatch.setattr(
        "leagent.library.retrieval.get_knowledge_retrieval_pipeline",
        lambda: Pipeline(),
    )

    block = await KnowledgeContextSource().resolve(
        ResolveContext(query="create a document outline", user_id=uuid4())
    )

    assert block is None
