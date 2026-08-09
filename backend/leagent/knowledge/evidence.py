"""Retrieval evidence gate and stable citation construction."""

from __future__ import annotations

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
        score = float(item.score or 0.0)
        if score < min_score:
            continue
        try:
            citations.append(
                Citation(
                    citation_id=f"K{len(citations) + 1}",
                    file_id=item.file_id,
                    file_name=str(item.file_name or ""),
                    chunk_id=item.chunk_id,
                    start_offset=int(item.start_offset or 0),
                    end_offset=int(item.end_offset or 0),
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
        from leagent.utils.metrics import get_metrics

        get_metrics().record_knowledge_evidence(bundle.status.value)
    except Exception:  # noqa: BLE001 - observability must not block answers
        pass
    return bundle


__all__ = [
    "Citation",
    "EvidenceBundle",
    "EvidenceStatus",
    "build_evidence",
]
