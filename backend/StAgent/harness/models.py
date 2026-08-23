"""Core data models for the Agent Harness system.

All harness state flows through these Pydantic models, enabling
JSON serialization, checkpoint persistence, and API exposure.
"""

from __future__ import annotations

import time
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

# ── Enums ──────────────────────────────────────────────────────────────────


class EpisodeStatus(StrEnum):
    """Lifecycle state of a TaskEpisode."""

    PENDING = "pending"
    RUNNING = "running"
    EVALUATING = "evaluating"
    PASSED = "passed"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


class FindingSeverity(StrEnum):
    """Impact level of a finding."""

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


class EpisodeOutcome(StrEnum):
    """Final disposition after evaluation."""

    PASS = "pass"
    FAIL = "fail"
    PARTIAL = "partial"
    BLOCKED = "blocked"


# ── Value objects ──────────────────────────────────────────────────────────


class AcceptanceCriteria(BaseModel):
    """Single acceptance check for a TaskEpisode."""

    id: str = Field(default_factory=lambda: uuid4().hex[:8])
    description: str
    check_type: str = "manual"  # manual | tool_success | output_contains | custom
    check_params: dict[str, Any] = Field(default_factory=dict)
    met: bool = False
    evidence: str = ""


class Finding(BaseModel):
    """A structured issue discovered during task execution."""

    id: str = Field(default_factory=lambda: uuid4().hex[:8])
    severity: FindingSeverity = FindingSeverity.MEDIUM
    category: str = "general"  # logic_bug | security | performance | style | general
    title: str
    description: str = ""
    file_path: str | None = None
    line_range: tuple[int, int] | None = None
    tool_name: str | None = None
    evidence: str = ""
    suggested_fix: str = ""
    resolved: bool = False
    created_at: float = Field(default_factory=time.time)


class RepairAction(BaseModel):
    """A single corrective action within a repair plan."""

    id: str = Field(default_factory=lambda: uuid4().hex[:8])
    description: str
    tool_name: str | None = None
    tool_params: dict[str, Any] = Field(default_factory=dict)
    priority: int = 0
    status: str = "pending"  # pending | in_progress | done | skipped
    result: str = ""


class RepairPlan(BaseModel):
    """Structured corrective plan generated on task failure."""

    id: str = Field(default_factory=lambda: uuid4().hex[:8])
    episode_id: str
    reason: str
    actions: list[RepairAction] = Field(default_factory=list)
    created_at: float = Field(default_factory=time.time)
    applied: bool = False


class ValidationResult(BaseModel):
    """Result of a pre/post-edit validation check."""

    passed: bool
    check_name: str
    message: str = ""
    details: dict[str, Any] = Field(default_factory=dict)
    timestamp: float = Field(default_factory=time.time)


class SessionScore(BaseModel):
    """Aggregated quality score for a session or task episode."""

    episode_id: str
    session_id: str
    overall: float = 0.0  # 0.0-1.0
    dimensions: dict[str, float] = Field(default_factory=dict)
    # tool_success_rate, finding_density, acceptance_rate, efficiency
    findings_count: int = 0
    critical_findings: int = 0
    acceptance_met: int = 0
    acceptance_total: int = 0
    duration_ms: int = 0
    tool_calls: int = 0
    tool_failures: int = 0
    outcome: EpisodeOutcome = EpisodeOutcome.PASS


# ── Root aggregate ─────────────────────────────────────────────────────────


class TaskEpisode(BaseModel):
    """One goal with one acceptance boundary — the harness unit of work.

    A TaskEpisode wraps an agent turn (or series of turns within one goal)
    and tracks: the objective, acceptance criteria, findings discovered
    during work, the final outcome evaluation, and optional repair plans.
    """

    id: str = Field(default_factory=lambda: uuid4().hex[:12])
    session_id: str = ""
    user_id: str = ""
    goal: str = ""
    status: EpisodeStatus = EpisodeStatus.PENDING
    outcome: EpisodeOutcome | None = None

    # Acceptance criteria
    criteria: list[AcceptanceCriteria] = Field(default_factory=list)

    # Findings captured during execution
    findings: list[Finding] = Field(default_factory=list)

    # Repair plans (generated on failure)
    repair_plans: list[RepairPlan] = Field(default_factory=list)

    # Validation history
    validations: list[ValidationResult] = Field(default_factory=list)

    # Score
    score: SessionScore | None = None

    # Timing
    started_at: float | None = None
    completed_at: float | None = None

    # Context
    tool_calls: list[str] = Field(default_factory=list)
    tool_results: list[dict[str, Any]] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def duration_ms(self) -> int:
        if self.started_at and self.completed_at:
            return int((self.completed_at - self.started_at) * 1000)
        return 0

    @property
    def is_terminal(self) -> bool:
        return self.status in (
            EpisodeStatus.PASSED,
            EpisodeStatus.FAILED,
            EpisodeStatus.BLOCKED,
            EpisodeStatus.CANCELLED,
        )

    @property
    def acceptance_rate(self) -> float:
        if not self.criteria:
            return 1.0
        met = sum(1 for c in self.criteria if c.met)
        return met / len(self.criteria)


__all__ = [
    "AcceptanceCriteria",
    "EpisodeOutcome",
    "EpisodeStatus",
    "Finding",
    "FindingSeverity",
    "RepairAction",
    "RepairPlan",
    "SessionScore",
    "TaskEpisode",
    "ValidationResult",
]
