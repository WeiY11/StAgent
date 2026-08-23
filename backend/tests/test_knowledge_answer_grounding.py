"""Answer-level citation validation for knowledge-context responses."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from StAgent.context.sources.base import ResolveContext
from StAgent.context.sources.knowledge_context import KnowledgeContextSource
from StAgent.knowledge.evaluation import evaluate_citation_validation
from StAgent.knowledge.evidence import (
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
        "StAgent.library.retrieval.get_knowledge_retrieval_pipeline",
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
        "StAgent.library.retrieval.get_knowledge_retrieval_pipeline",
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
        "StAgent.library.retrieval.get_knowledge_retrieval_pipeline",
        lambda: Pipeline(),
    )

    block = await KnowledgeContextSource().resolve(
        ResolveContext(query="create a document outline", user_id=uuid4())
    )

    assert block is None


@pytest.mark.asyncio
async def test_query_naming_session_attachment_stem_opens_knowledge_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Pipeline:
        async def search(self, _options: object) -> list[SimpleNamespace]:
            return [
                SimpleNamespace(
                    snippet="Attachment-backed excerpt.",
                    text="Attachment-backed excerpt.",
                    file_name="notes.txt",
                    file_id=uuid4(),
                    chunk_id=uuid4(),
                    score=0.9,
                    start_offset=0,
                    end_offset=25,
                )
            ]

    monkeypatch.setattr(
        "StAgent.library.retrieval.get_knowledge_retrieval_pipeline",
        lambda: Pipeline(),
    )

    class SessionManager:
        async def list_attachments(self, _sid: object, *, user_id: object = None):
            return [SimpleNamespace(filename="Q3-report.pdf", storage_path="x")]

    block = await KnowledgeContextSource().resolve(
        ResolveContext(
            query="summarize the Q3-report for me",
            user_id=uuid4(),
            session_id=uuid4(),
            session_manager=SessionManager(),  # type: ignore[arg-type]
        )
    )

    # The attachment stem match opened the gate, so evidence was injected
    # even though no keyword hint was present.
    assert block is not None
    evidence = block.metadata["knowledge_evidence"]
    assert evidence.status is EvidenceStatus.SUFFICIENT


@pytest.mark.asyncio
async def test_attachment_gate_without_session_or_manager_stays_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Pipeline:
        async def search(self, _options: object) -> list[SimpleNamespace]:
            return []

    monkeypatch.setattr(
        "StAgent.library.retrieval.get_knowledge_retrieval_pipeline",
        lambda: Pipeline(),
    )

    # No keyword hints, no session context: the gate must stay closed and
    # must not fall back to a fail-closed refusal block.
    block = await KnowledgeContextSource().resolve(
        ResolveContext(query="tell me about quarterly numbers", user_id=uuid4())
    )

    assert block is None
