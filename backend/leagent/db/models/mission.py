"""Mission persistence models — durable goal-scoped autonomous execution.

A :class:`Mission` is the durable execution unit for mission mode (plan →
wave-scheduled steps → verification gate → bounded continuation). The
``agent_mission_steps`` table is the working state of the *current* plan
version (``plan_id``): a replan writes new rows under ``plan_id + 1``
instead of mutating history, so failed attempts remain auditable.

The runtime state machine lives in :mod:`leagent.mission.runner`; these
models only define what is persisted.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from uuid import UUID

from sqlmodel import Column, Field, Text, UniqueConstraint

from leagent.db.models.base import BaseModel, enum_value_column


class MissionStatus(str, Enum):
    """Mission lifecycle."""

    PLANNING = "planning"
    READY = "ready"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    ABORTED = "aborted"
    EXPIRED = "expired"


class MissionAutonomy(str, Enum):
    """How much autonomy the mission gets before human approval."""

    FULL = "full"
    STEP_CONFIRM = "step_confirm"
    READONLY = "readonly"


class MissionVerifyLevel(str, Enum):
    """Verification gate strength."""

    NONE = "none"
    HEURISTIC = "heuristic"
    LLM_CRITIC = "llm_critic"


class MissionStepKind(str, Enum):
    """Coarse step category used by the verification gate."""

    CODE = "code"
    RESEARCH = "research"
    DATA = "data"
    FILE = "file"
    GENERIC = "generic"


class MissionStepStatus(str, Enum):
    """Per-step lifecycle."""

    PENDING = "pending"
    RUNNING = "running"
    VERIFYING = "verifying"
    WAITING_APPROVAL = "waiting_approval"
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    BLOCKED = "blocked"


class Mission(BaseModel, table=True):
    """Durable mission (goal-scoped autonomous execution unit)."""

    __tablename__ = "agent_missions"

    user_id: UUID | None = Field(default=None, foreign_key="users.id", index=True)
    session_id: UUID | None = Field(default=None, index=True)
    goal: str = Field(max_length=4000)
    agent_id: str | None = Field(default=None, max_length=100)
    status: MissionStatus = Field(
        default=MissionStatus.PLANNING,
        sa_column=enum_value_column(MissionStatus, index=True),
    )
    autonomy: MissionAutonomy = Field(
        default=MissionAutonomy.FULL,
        sa_column=enum_value_column(MissionAutonomy),
    )
    verify_level: MissionVerifyLevel = Field(
        default=MissionVerifyLevel.HEURISTIC,
        sa_column=enum_value_column(MissionVerifyLevel),
    )
    expected_output: str | None = Field(default=None, sa_column=Column(Text))
    plan_id: int = Field(default=0)
    budget_json: str | None = Field(default=None, sa_column=Column(Text))
    usage_json: str | None = Field(default=None, sa_column=Column(Text))
    pause_reason: str | None = Field(default=None, max_length=200)
    error: str | None = Field(default=None, sa_column=Column(Text))
    started_at: datetime | None = Field(default=None)
    completed_at: datetime | None = Field(default=None)


class MissionStep(BaseModel, table=True):
    """A single step of the current mission plan version."""

    __tablename__ = "agent_mission_steps"
    __table_args__ = (
        UniqueConstraint(
            "mission_id",
            "plan_id",
            "step_no",
            name="ux_agent_mission_steps_plan_step",
        ),
    )

    mission_id: UUID = Field(foreign_key="agent_missions.id", index=True)
    step_no: int
    plan_id: int = Field(default=0)
    description: str = Field(max_length=4000)
    kind: MissionStepKind = Field(
        default=MissionStepKind.GENERIC,
        sa_column=enum_value_column(MissionStepKind),
    )
    depends_on: str | None = Field(default=None, sa_column=Column(Text))
    wave: int = Field(default=0)
    status: MissionStepStatus = Field(
        default=MissionStepStatus.PENDING,
        sa_column=enum_value_column(MissionStepStatus, index=True),
    )
    attempt: int = Field(default=0)
    verify_result: str | None = Field(default=None, sa_column=Column(Text))
    segment_summary: str | None = Field(default=None, sa_column=Column(Text))
    checkpoint_id: str | None = Field(default=None, max_length=200)
    error: str | None = Field(default=None, sa_column=Column(Text))
    started_at: datetime | None = Field(default=None)
    completed_at: datetime | None = Field(default=None)


__all__ = [
    "Mission",
    "MissionStep",
    "MissionStatus",
    "MissionAutonomy",
    "MissionVerifyLevel",
    "MissionStepKind",
    "MissionStepStatus",
]
