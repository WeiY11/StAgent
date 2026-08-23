"""Mission API — create, inspect and control goal-scoped autonomous runs.

Thin REST surface over :class:`~StAgent.mission.service.MissionService`
(persistence via ``MissionRepository``, execution via ``MissionRunner``).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from StAgent.db import DatabaseService, get_database_service
from StAgent.db.models.mission import (
    Mission,
    MissionAutonomy,
    MissionStatus,
    MissionStep,
    MissionStepKind,
    MissionStepStatus,
    MissionVerifyLevel,
)
from StAgent.db.sqlite_compat import load_entity_by_id, same_user_id
from StAgent.mission.service import MissionService, get_mission_service
from StAgent.mission.types import MissionBudget, parse_depends
from StAgent.services.auth import CurrentUserId

router = APIRouter()

_TERMINAL_MISSION_STATUSES = frozenset({
    MissionStatus.COMPLETED,
    MissionStatus.FAILED,
    MissionStatus.ABORTED,
    MissionStatus.EXPIRED,
})


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class MissionBudgetIn(BaseModel):
    """User-supplied mission quotas (all optional, sensible defaults)."""

    max_steps: int = Field(default=20, ge=1, le=100)
    max_segments_per_step: int = Field(default=3, ge=1, le=10)
    max_verify_retries: int = Field(default=2, ge=0, le=10)
    max_total_tokens: int = Field(default=4_000_000, ge=1_000)
    max_cost_usd: float | None = Field(default=None, gt=0)
    deadline: datetime | None = None


class MissionCreateRequest(BaseModel):
    """Create + immediately start a mission."""

    goal: str = Field(min_length=1, max_length=4000)
    expected_output: str | None = Field(default=None, max_length=8000)
    agent_id: str | None = Field(default=None, max_length=100)
    session_id: UUID | None = None
    autonomy: MissionAutonomy = MissionAutonomy.FULL
    verify_level: MissionVerifyLevel = MissionVerifyLevel.HEURISTIC
    budget: MissionBudgetIn | None = None


class MissionStepRead(BaseModel):
    step_no: int
    plan_id: int
    description: str
    kind: MissionStepKind
    depends_on: list[int]
    wave: int
    status: MissionStepStatus
    attempt: int
    verify_result: dict[str, Any] | None = None
    segment_summary: str | None = None
    checkpoint_id: str | None = None
    error: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None


class MissionRead(BaseModel):
    id: UUID
    goal: str
    expected_output: str | None = None
    agent_id: str | None = None
    session_id: UUID | None = None
    status: MissionStatus
    autonomy: MissionAutonomy
    verify_level: MissionVerifyLevel
    plan_id: int
    pause_reason: str | None = None
    error: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    budget: dict[str, Any] | None = None
    usage: dict[str, Any] | None = None
    running: bool = False
    steps: list[MissionStepRead] | None = None


class MissionStepApproveRequest(BaseModel):
    approved: bool
    note: str = Field(default="", max_length=4000)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _loads_object(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _step_to_read(step: MissionStep) -> MissionStepRead:
    return MissionStepRead(
        step_no=int(step.step_no),
        plan_id=int(step.plan_id),
        description=str(step.description),
        kind=step.kind,
        depends_on=parse_depends(step.depends_on),
        wave=int(step.wave or 0),
        status=step.status,
        attempt=int(step.attempt or 0),
        verify_result=_loads_object(step.verify_result),
        segment_summary=step.segment_summary,
        checkpoint_id=step.checkpoint_id,
        error=step.error,
        started_at=step.started_at,
        completed_at=step.completed_at,
    )


def _to_read(
    mission: Mission,
    service: MissionService,
    *,
    steps: list[MissionStep] | None = None,
) -> MissionRead:
    return MissionRead(
        id=mission.id,
        goal=mission.goal,
        expected_output=mission.expected_output,
        agent_id=mission.agent_id,
        session_id=mission.session_id,
        status=mission.status,
        autonomy=mission.autonomy,
        verify_level=mission.verify_level,
        plan_id=int(mission.plan_id or 0),
        pause_reason=mission.pause_reason,
        error=mission.error,
        started_at=mission.started_at,
        completed_at=mission.completed_at,
        created_at=mission.created_at,
        updated_at=mission.updated_at,
        budget=_loads_object(mission.budget_json),
        usage=_loads_object(mission.usage_json),
        running=service.is_running(mission.id),
        steps=[_step_to_read(s) for s in steps] if steps is not None else None,
    )


async def _require_mission(
    mission_id: UUID,
    user_id: UUID,
    db: DatabaseService,
) -> Mission:
    async with db.session() as session:
        mission = await load_entity_by_id(session, Mission, mission_id, parent_table="agent_missions")
    if mission is None or not same_user_id(mission.user_id, user_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Mission not found")
    return mission


def _budget_to_model(budget_in: MissionBudgetIn | None) -> str:
    budget = budget_in or MissionBudgetIn()
    deadline = budget.deadline
    if deadline is not None and deadline.tzinfo is not None:
        deadline = deadline.astimezone(UTC).replace(tzinfo=None)
    return MissionBudget(
        max_steps=budget.max_steps,
        max_segments_per_step=budget.max_segments_per_step,
        max_verify_retries=budget.max_verify_retries,
        max_total_tokens=budget.max_total_tokens,
        max_cost_usd=budget.max_cost_usd,
        deadline=deadline,
    ).to_json()


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("", response_model=MissionRead, status_code=status.HTTP_201_CREATED)
async def create_mission(
    body: MissionCreateRequest,
    user_id: CurrentUserId,
    mission_svc: Annotated[MissionService, Depends(get_mission_service)],
    db: Annotated[DatabaseService, Depends(get_database_service)],
) -> MissionRead:
    """Create a mission and immediately start planning + execution."""
    mission = Mission(
        user_id=user_id,
        session_id=body.session_id,
        goal=body.goal.strip(),
        agent_id=body.agent_id,
        status=MissionStatus.PLANNING,
        autonomy=body.autonomy,
        verify_level=body.verify_level,
        expected_output=body.expected_output,
        budget_json=_budget_to_model(body.budget),
    )
    mission = await mission_svc.store.create(mission)
    await mission_svc.ensure_running(mission)
    fresh = await _require_mission(mission.id, user_id, db)
    return _to_read(fresh, mission_svc)


@router.get("", response_model=list[MissionRead])
async def list_missions(
    user_id: CurrentUserId,
    mission_svc: Annotated[MissionService, Depends(get_mission_service)],
    status_filter: MissionStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[MissionRead]:
    missions = await mission_svc.store.list_for_user(user_id, status=status_filter, limit=limit)
    return [_to_read(m, mission_svc) for m in missions]


@router.get("/{mission_id}", response_model=MissionRead)
async def get_mission(
    mission_id: UUID,
    user_id: CurrentUserId,
    mission_svc: Annotated[MissionService, Depends(get_mission_service)],
    db: Annotated[DatabaseService, Depends(get_database_service)],
) -> MissionRead:
    mission = await _require_mission(mission_id, user_id, db)
    got = await mission_svc.store.get_with_steps(mission_id)
    if got is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Mission not found")
    mission, steps = got
    return _to_read(mission, mission_svc, steps=steps)


@router.post("/{mission_id}/pause", response_model=MissionRead)
async def pause_mission(
    mission_id: UUID,
    user_id: CurrentUserId,
    mission_svc: Annotated[MissionService, Depends(get_mission_service)],
    db: Annotated[DatabaseService, Depends(get_database_service)],
) -> MissionRead:
    await _require_mission(mission_id, user_id, db)
    await mission_svc.pause(mission_id)
    mission = await _require_mission(mission_id, user_id, db)
    return _to_read(mission, mission_svc)


@router.post("/{mission_id}/resume", response_model=MissionRead)
async def resume_mission(
    mission_id: UUID,
    user_id: CurrentUserId,
    mission_svc: Annotated[MissionService, Depends(get_mission_service)],
    db: Annotated[DatabaseService, Depends(get_database_service)],
) -> MissionRead:
    await _require_mission(mission_id, user_id, db)
    try:
        await mission_svc.resume(mission_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    mission = await _require_mission(mission_id, user_id, db)
    return _to_read(mission, mission_svc)


@router.post("/{mission_id}/abort", response_model=MissionRead)
async def abort_mission(
    mission_id: UUID,
    user_id: CurrentUserId,
    mission_svc: Annotated[MissionService, Depends(get_mission_service)],
    db: Annotated[DatabaseService, Depends(get_database_service)],
) -> MissionRead:
    await _require_mission(mission_id, user_id, db)
    await mission_svc.abort(mission_id)
    mission = await _require_mission(mission_id, user_id, db)
    return _to_read(mission, mission_svc)


@router.post("/{mission_id}/steps/{step_no}/approve", response_model=MissionRead)
async def approve_mission_step(
    mission_id: UUID,
    step_no: int,
    body: MissionStepApproveRequest,
    user_id: CurrentUserId,
    mission_svc: Annotated[MissionService, Depends(get_mission_service)],
    db: Annotated[DatabaseService, Depends(get_database_service)],
) -> MissionRead:
    await _require_mission(mission_id, user_id, db)
    try:
        await mission_svc.approve_step(
            mission_id, step_no, approved=body.approved, note=body.note
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    mission = await _require_mission(mission_id, user_id, db)
    return _to_read(mission, mission_svc)


@router.delete("/{mission_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_mission(
    mission_id: UUID,
    user_id: CurrentUserId,
    mission_svc: Annotated[MissionService, Depends(get_mission_service)],
    db: Annotated[DatabaseService, Depends(get_database_service)],
) -> None:
    await _require_mission(mission_id, user_id, db)
    if mission_svc.is_running(mission_id):
        await mission_svc.abort(mission_id)
    await mission_svc.store.delete(mission_id)
