"""Retrieval evidence gate and stable citation construction."""

from __future__ import annotations

import re
from collections.abc import Sequence  # noqa: TC003 - runtime sequence annotations are public
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import UUID  # noqa: TC003 - citation payload carries UUIDs at runtime


class EvidenceStatus(StrEnum):
    SUFFICIENT = "sufficient"
    INSUFFICIENT = "insufficient"
    DEGRADED = "degraded"


@dataclass(frozen=True)
class Citation:
    citation_id: str
    file_id: UUID
    file_name: str
    chunk_id: UUID
    start_offset: int
    end_offset: int
    score: float


@dataclass(frozen=True)
class EvidenceBundle:
    status: EvidenceStatus
    citations: tuple[Citation, ...]
    reason: str | None = None


@dataclass(frozen=True)
class AnswerGroundingResult:
    """Citation-token validation result for one generated answer.

    This checks that references name evidence supplied to the model.  It does
    not assert textual entailment, so callers must not reinterpret a passing
    result as a semantic hallucination measurement.
    """

    accepted: bool
    referenced_citation_ids: tuple[str, ...]
    invalid_citation_ids: tuple[str, ...]
    reason: str | None = None


_CITATION_TOKEN_RE = re.compile(r"\[(K\d+)\]")
_CITATION_ID_RE = re.compile(r"K\d+\Z")


def _field(item: Any, name: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)


def validate_answer_citations(
    answer: str,
    evidence: EvidenceBundle,
    *,
    strict: bool,
) -> AnswerGroundingResult:
    """Validate stable ``[K#]`` references in a generated answer.

    Strict mode requires sufficient retrieval evidence and at least one valid
    reference whenever a knowledge attachment was supplied.  Non-strict mode
    permits an uncited answer, but never accepts an explicit unknown citation.
    """
    referenced = tuple(dict.fromkeys(_CITATION_TOKEN_RE.findall(answer or "")))
    valid_ids = {citation.citation_id for citation in evidence.citations}
    invalid = tuple(citation_id for citation_id in referenced if citation_id not in valid_ids)
    if invalid:
        return AnswerGroundingResult(
            accepted=False,
            referenced_citation_ids=referenced,
            invalid_citation_ids=invalid,
            reason="invalid_citation",
        )
    if strict and evidence.status is not EvidenceStatus.SUFFICIENT:
        return AnswerGroundingResult(
            accepted=False,
            referenced_citation_ids=referenced,
            invalid_citation_ids=(),
            reason="evidence_not_sufficient",
        )
    if strict and not referenced:
        return AnswerGroundingResult(
            accepted=False,
            referenced_citation_ids=(),
            invalid_citation_ids=(),
            reason="missing_citation",
        )
    return AnswerGroundingResult(
        accepted=True,
        referenced_citation_ids=referenced,
        invalid_citation_ids=(),
    )


def render_citation_legend(
    evidence: EvidenceBundle,
    citation_ids: Sequence[str],
) -> str:
    """Render a user-visible source legend for validated answer citations."""
    wanted = set(citation_ids)
    lines: list[str] = []
    for citation in evidence.citations:
        if citation.citation_id not in wanted:
            continue
        label = citation.file_name or "knowledge document"
        lines.append(
            f"- [{citation.citation_id}] {label} "
            f"(excerpt {citation.start_offset}-{citation.end_offset})"
        )
    return "\n\nSources:\n" + "\n".join(lines) if lines else ""


def build_evidence(
    results: Sequence[Any],
    *,
    min_results: int = 1,
    min_score: float = 0.0,
    strict: bool = True,
) -> EvidenceBundle:
    """Build citations and refuse strict answers without grounded hits.

    This is a retrieval gate only; it does not claim that a generated answer
    cited every fact.  The answer layer must validate citation ids separately.
    """
    citations: list[Citation] = []
    for item in results:
        try:
            score = float(_field(item, "score", 0.0) or 0.0)
        except (TypeError, ValueError):
            continue
        if score < min_score:
            continue
        try:
            citations.append(
                Citation(
                    citation_id=f"K{len(citations) + 1}",
                    file_id=_field(item, "file_id"),
                    file_name=str(_field(item, "file_name", "") or ""),
                    chunk_id=_field(item, "chunk_id"),
                    start_offset=int(_field(item, "start_offset", 0) or 0),
                    end_offset=int(_field(item, "end_offset", 0) or 0),
                    score=score,
                )
            )
        except (TypeError, ValueError, AttributeError):
            continue
    if len(citations) >= max(1, min_results):
        bundle = EvidenceBundle(EvidenceStatus.SUFFICIENT, tuple(citations))
    else:
        status = EvidenceStatus.INSUFFICIENT if strict else EvidenceStatus.DEGRADED
        bundle = EvidenceBundle(status, tuple(citations), reason="no_sufficient_retrieval_evidence")
    try:
        from StAgent.utils.metrics import get_metrics

        get_metrics().record_knowledge_evidence(bundle.status.value)
    except Exception:  # noqa: BLE001 - observability must not block answers
        pass
    return bundle


def evidence_from_knowledge_search_payload(payload: Any) -> EvidenceBundle | None:
    """Build an evidence bundle from a ``knowledge_search`` tool response.

    Tool responses cross the model boundary as JSON, so this parser is
    deliberately strict about IDs and offsets.  A malformed search response
    becomes insufficient evidence instead of silently disabling answer-level
    grounding on the next agent turn.
    """
    if not isinstance(payload, dict) or payload.get("action") != "search":
        return None

    raw_status = str(payload.get("evidence_status") or "").strip()
    try:
        reported_status = EvidenceStatus(raw_status)
    except ValueError:
        reported_status = EvidenceStatus.INSUFFICIENT

    results_by_citation: dict[str, dict[str, Any]] = {}
    raw_results = payload.get("results")
    if isinstance(raw_results, list):
        for result in raw_results:
            if not isinstance(result, dict):
                continue
            citation_id = str(result.get("citation_id") or "")
            if _CITATION_ID_RE.fullmatch(citation_id):
                results_by_citation[citation_id] = result

    citations: list[Citation] = []
    seen_source_ids: set[str] = set()
    raw_citations = payload.get("citations")
    if isinstance(raw_citations, list):
        for raw_citation in raw_citations:
            if not isinstance(raw_citation, dict):
                continue
            citation_id = str(raw_citation.get("citation_id") or "")
            if not _CITATION_ID_RE.fullmatch(citation_id) or citation_id in seen_source_ids:
                continue
            try:
                start_offset = int(raw_citation.get("start_offset", 0))
                end_offset = int(raw_citation.get("end_offset", 0))
                if start_offset < 0 or end_offset < start_offset:
                    continue
                result = results_by_citation.get(citation_id, {})
                citations.append(
                    Citation(
                        citation_id=citation_id,
                        file_id=UUID(str(raw_citation["file_id"])),
                        file_name=str(result.get("name") or ""),
                        chunk_id=UUID(str(raw_citation["chunk_id"])),
                        start_offset=start_offset,
                        end_offset=end_offset,
                        score=float(result.get("score", 0.0) or 0.0),
                    )
                )
                seen_source_ids.add(citation_id)
            except (KeyError, TypeError, ValueError):
                continue

    if reported_status is EvidenceStatus.SUFFICIENT and citations:
        return EvidenceBundle(EvidenceStatus.SUFFICIENT, tuple(citations))
    if reported_status is EvidenceStatus.DEGRADED:
        return EvidenceBundle(
            EvidenceStatus.DEGRADED,
            tuple(citations),
            reason="tool_reported_degraded_evidence",
        )
    return EvidenceBundle(
        EvidenceStatus.INSUFFICIENT,
        tuple(citations),
        reason="tool_reported_insufficient_or_malformed_evidence",
    )


def merge_evidence_bundles(
    existing: EvidenceBundle | None,
    incoming: EvidenceBundle,
) -> tuple[EvidenceBundle, dict[str, str]]:
    """Merge evidence while preventing ``[K#]`` collisions across searches.

    The mapping rewrites only the incoming tool response.  Existing citations
    have already been shown to the model and therefore retain their IDs.
    """
    citations: list[Citation] = []
    identity_to_citation: dict[tuple[UUID, UUID, int, int], Citation] = {}
    used_ids: set[str] = set()

    for citation in (existing.citations if existing is not None else ()):
        identity = (
            citation.file_id,
            citation.chunk_id,
            citation.start_offset,
            citation.end_offset,
        )
        if identity in identity_to_citation:
            continue
        identity_to_citation[identity] = citation
        citations.append(citation)
        used_ids.add(citation.citation_id)

    next_index = max(
        (
            int(citation_id[1:])
            for citation_id in used_ids
            if _CITATION_ID_RE.fullmatch(citation_id)
        ),
        default=0,
    ) + 1
    rewritten_ids: dict[str, str] = {}

    for citation in incoming.citations:
        identity = (
            citation.file_id,
            citation.chunk_id,
            citation.start_offset,
            citation.end_offset,
        )
        existing_citation = identity_to_citation.get(identity)
        if existing_citation is not None:
            rewritten_ids[citation.citation_id] = existing_citation.citation_id
            continue

        candidate_id = citation.citation_id
        if candidate_id in used_ids or not _CITATION_ID_RE.fullmatch(candidate_id):
            candidate_id = f"K{next_index}"
            next_index += 1
        rewritten_ids[citation.citation_id] = candidate_id
        normalized = Citation(
            citation_id=candidate_id,
            file_id=citation.file_id,
            file_name=citation.file_name,
            chunk_id=citation.chunk_id,
            start_offset=citation.start_offset,
            end_offset=citation.end_offset,
            score=citation.score,
        )
        identity_to_citation[identity] = normalized
        citations.append(normalized)
        used_ids.add(candidate_id)

    statuses = [bundle.status for bundle in (existing, incoming) if bundle is not None]
    if citations and EvidenceStatus.SUFFICIENT in statuses:
        status = EvidenceStatus.SUFFICIENT
        reason = None
    elif EvidenceStatus.DEGRADED in statuses:
        status = EvidenceStatus.DEGRADED
        reason = "retrieval_evidence_degraded"
    else:
        status = EvidenceStatus.INSUFFICIENT
        reason = "no_sufficient_retrieval_evidence"
    return EvidenceBundle(status, tuple(citations), reason=reason), rewritten_ids


def rewrite_knowledge_search_citation_ids(
    payload: dict[str, Any],
    citation_ids: dict[str, str],
) -> None:
    """Apply collision-free citation IDs to the JSON the model will read."""
    for field in ("citations", "results"):
        entries = payload.get(field)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            current = str(entry.get("citation_id") or "")
            if current in citation_ids:
                entry["citation_id"] = citation_ids[current]


__all__ = [
    "AnswerGroundingResult",
    "Citation",
    "EvidenceBundle",
    "EvidenceStatus",
    "build_evidence",
    "evidence_from_knowledge_search_payload",
    "merge_evidence_bundles",
    "render_citation_legend",
    "rewrite_knowledge_search_citation_ids",
    "validate_answer_citations",
]
