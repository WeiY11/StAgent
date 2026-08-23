"""Runtime types for mission orchestration.

The persistence models live in :mod:`StAgent.db.models.mission`; these
dataclasses are the DB-agnostic working shapes used by
:class:`~StAgent.mission.runner.MissionRunner`: the budget/usage ledgers,
the validated plan (dependency graph + parallel waves, reusing the
scheduling helpers from :mod:`StAgent.agent.planner`), and the segment /
verification results flowing through the injected seams.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from StAgent.agent.base import PlanStep
from StAgent.agent.planner import get_parallel_groups

# Segment terminal reasons that the mission loop may continue with a fresh
# context window (mirrors the kernel's RESUMABLE_CHECKPOINT_REASONS).
CONTINUABLE_SEGMENT_REASONS: frozenset[str] = frozenset({"max_turns", "token_budget_exceeded", "prompt_too_long"})

_TERMINAL_STEP_STATUSES: frozenset[str] = frozenset({"passed", "skipped", "failed", "blocked"})


@dataclass(frozen=True)
class MissionBudget:
    """Mission-scoped quotas. Any cap hit pauses the mission."""

    max_steps: int = 20
    max_segments_per_step: int = 3
    max_verify_retries: int = 2
    max_total_tokens: int = 4_000_000
    max_cost_usd: float | None = None
    deadline: datetime | None = None

    def to_json(self) -> str:
        """Serialize to the ``agent_missions.budget_json`` column."""
        return json.dumps(
            {
                "max_steps": self.max_steps,
                "max_segments_per_step": self.max_segments_per_step,
                "max_verify_retries": self.max_verify_retries,
                "max_total_tokens": self.max_total_tokens,
                "max_cost_usd": self.max_cost_usd,
                "deadline": self.deadline.isoformat() if self.deadline else None,
            }
        )

    @classmethod
    def from_json(cls, raw: str | None) -> MissionBudget:
        """Parse the ``agent_missions.budget_json`` column (defaults on empty)."""
        if not raw:
            return cls()
        data = json.loads(raw)
        deadline_raw = data.get("deadline")
        deadline = datetime.fromisoformat(deadline_raw) if deadline_raw else None
        max_cost_raw = data.get("max_cost_usd")
        return cls(
            max_steps=int(data.get("max_steps", cls.max_steps)),
            max_segments_per_step=int(data.get("max_segments_per_step", cls.max_segments_per_step)),
            max_verify_retries=int(data.get("max_verify_retries", cls.max_verify_retries)),
            max_total_tokens=int(data.get("max_total_tokens", cls.max_total_tokens)),
            max_cost_usd=float(max_cost_raw) if max_cost_raw is not None else None,
            deadline=deadline,
        )

    def expired(self, *, now: datetime | None = None) -> bool:
        """True when the deadline has passed (naive UTC comparison)."""
        if self.deadline is None:
            return False
        now = now or datetime.now(UTC).replace(tzinfo=None)
        deadline = self.deadline
        if deadline.tzinfo is not None:
            deadline = deadline.astimezone(UTC).replace(tzinfo=None)
        return now >= deadline


@dataclass
class SegmentResult:
    """Outcome of one bounded segment (one ``run_loop`` invocation).

    ``changed_paths`` / ``shell_blobs`` / ``activity`` carry the tool
    activity collected from the run stream (mirroring the sub-agent
    activity collector) so the heuristic verification gate can check
    toolchain verification without re-reading the transcript.
    """

    reason: str = "completed"
    text: str = ""
    produced_files: list[str] = field(default_factory=list)
    changed_paths: list[str] = field(default_factory=list)
    shell_blobs: list[str] = field(default_factory=list)
    activity: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    checkpoint_id: str | None = None

    @property
    def continuable(self) -> bool:
        """True when the segment can be continued with a fresh context window."""
        return self.reason in CONTINUABLE_SEGMENT_REASONS

    @property
    def succeeded(self) -> bool:
        return self.reason == "completed"

    @property
    def tokens_used(self) -> int:
        """Best-effort token total across common usage dict shapes."""
        total = self.usage.get("total_tokens")
        if total is not None:
            return int(total)
        return int(self.usage.get("input_tokens") or 0) + int(self.usage.get("output_tokens") or 0)

    @property
    def cost_usd(self) -> float:
        """Best-effort cost in USD carried by the usage payload (0 when absent)."""
        try:
            return float(self.usage.get("cost_usd") or 0.0)
        except (TypeError, ValueError):
            return 0.0


@dataclass
class VerificationResult:
    """Verdict of the verification gate for one segment."""

    passed: bool
    feedback: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class MissionUsage:
    """Accumulated usage ledger, persisted on ``agent_missions.usage_json``."""

    segments: int = 0
    tokens: int = 0
    cost_usd: float = 0.0

    def add(self, result: SegmentResult) -> None:
        """Fold one segment's usage into the ledger."""
        self.segments += 1
        self.tokens += result.tokens_used
        self.cost_usd += result.cost_usd

    def over(self, budget: MissionBudget) -> bool:
        """True when any mission-level quota is exceeded."""
        over_tokens = self.tokens > budget.max_total_tokens
        over_cost = budget.max_cost_usd is not None and self.cost_usd > budget.max_cost_usd
        return over_tokens or over_cost

    def to_json(self) -> str:
        return json.dumps(
            {
                "segments": self.segments,
                "tokens": self.tokens,
                "cost_usd": self.cost_usd,
            }
        )

    @classmethod
    def from_json(cls, raw: str | None) -> MissionUsage:
        if not raw:
            return cls()
        data = json.loads(raw)
        return cls(
            segments=int(data.get("segments", 0)),
            tokens=int(data.get("tokens", 0)),
            cost_usd=float(data.get("cost_usd", 0.0)),
        )


@dataclass
class MissionStepState:
    """DB-agnostic working shape for one plan step."""

    step_no: int
    description: str
    kind: str = "generic"
    depends_on: list[int] = field(default_factory=list)
    wave: int = 0
    status: str = "pending"
    attempt: int = 0
    verify_result: dict[str, Any] | None = None
    segment_summary: str | None = None
    checkpoint_id: str | None = None

    @property
    def is_terminal(self) -> bool:
        return self.status in _TERMINAL_STEP_STATUSES

    @classmethod
    def from_row(cls, row: Any) -> MissionStepState:
        """Build state from a ``MissionStep`` ORM row (or any duck-typed row)."""
        return cls(
            step_no=int(row.step_no),
            description=str(row.description),
            kind=_enum_value(row.kind, "generic"),
            depends_on=parse_depends(row.depends_on),
            wave=int(row.wave or 0),
            status=_enum_value(row.status, "pending"),
            attempt=int(row.attempt or 0),
            verify_result=_parse_json_object(row.verify_result),
            segment_summary=row.segment_summary,
            checkpoint_id=row.checkpoint_id,
        )


@dataclass
class MissionPlan:
    """Validated plan: dependency graph plus wave computation."""

    goal: str
    expected_output: str = ""
    steps: list[MissionStepState] = field(default_factory=list)

    def validate(self, *, max_steps: int) -> list[str]:
        """Return human-readable validation errors (empty list = valid)."""
        errors: list[str] = []
        if not self.steps:
            errors.append("plan has no steps")
        if len(self.steps) > max_steps:
            errors.append(f"plan has {len(self.steps)} steps, exceeding max_steps={max_steps}")
        seen: set[int] = set()
        for step in self.steps:
            if step.step_no in seen:
                errors.append(f"duplicate step_no {step.step_no}")
            seen.add(step.step_no)
        for step in self.steps:
            errors.extend(
                f"step {step.step_no} depends on unknown step {dep}" for dep in step.depends_on if dep not in seen
            )
        if errors:
            return errors
        if _topological_order(self.steps) is None:
            errors.append("plan contains a dependency cycle")
        return errors

    def waves(self) -> list[list[MissionStepState]]:
        """Group steps into dependency-ordered waves (wave 0 first).

        Raises:
            ValueError: when the plan is invalid (call :meth:`validate` first).
        """
        errors = self.validate(max_steps=max(len(self.steps), 1))
        if errors:
            raise ValueError("invalid plan: " + "; ".join(errors))
        by_no = {s.step_no: s for s in self.steps}
        bridged = [
            PlanStep(
                id=s.step_no,
                description=s.description,
                depends_on=list(s.depends_on),
            )
            for s in self.steps
        ]
        result: list[list[MissionStepState]] = []
        for wave_idx, group in enumerate(get_parallel_groups(bridged)):
            wave: list[MissionStepState] = []
            for plan_step in group:
                state = by_no[plan_step.id]
                state.wave = wave_idx
                wave.append(state)
            result.append(wave)
        return result


def parse_depends(raw: Any) -> list[int]:
    """Parse a ``depends_on`` JSON column into ints (empty on malformed)."""
    if not raw:
        return []
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
        return [int(d) for d in data]
    except (TypeError, ValueError, json.JSONDecodeError):
        return []


def _parse_json_object(raw: Any) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
        return data if isinstance(data, dict) else None
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def _enum_value(value: Any, default: str) -> str:
    """Read an ORM enum column (or plain string) as its string value."""
    if value is None:
        return default
    if hasattr(value, "value"):
        return str(value.value)
    return str(value)


def _topological_order(steps: list[MissionStepState]) -> list[int] | None:
    """Kahn's algorithm; returns an order, or ``None`` when a cycle exists."""
    in_degree = {s.step_no: len(s.depends_on) for s in steps}
    adjacency: dict[int, list[int]] = {s.step_no: [] for s in steps}
    for step in steps:
        for dep in step.depends_on:
            adjacency[dep].append(step.step_no)
    queue = [step_no for step_no, deg in in_degree.items() if deg == 0]
    order: list[int] = []
    while queue:
        current = queue.pop(0)
        order.append(current)
        for dependent in adjacency[current]:
            in_degree[dependent] -= 1
            if in_degree[dependent] == 0:
                queue.append(dependent)
    if len(order) != len(steps):
        return None
    return order


__all__ = [
    "CONTINUABLE_SEGMENT_REASONS",
    "MissionBudget",
    "MissionPlan",
    "MissionStepState",
    "MissionUsage",
    "SegmentResult",
    "VerificationResult",
    "parse_depends",
]
