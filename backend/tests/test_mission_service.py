"""Unit tests for :mod:`StAgent.mission.service` (MissionService)."""

from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from StAgent.db.models.mission import Mission, MissionStatus, MissionStepStatus
from StAgent.mission.runner import AlwaysPassVerifier, StaticPlanner
from StAgent.mission.service import MissionService
from StAgent.mission.types import MissionPlan, MissionStepState, SegmentResult


class _FakeDb:
    """Minimal ``DatabaseService`` stand-in: just the ``session()`` factory."""

    def __init__(self) -> None:
        self._engine = create_async_engine("sqlite+aiosqlite://")
        self._session_factory = async_sessionmaker(
            self._engine, class_=AsyncSession, expire_on_commit=False
        )

    async def create_all(self) -> None:
        async with self._engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)

    def session(self) -> Any:
        """Match ``DatabaseService.session()``: commit on success, rollback on error."""

        @asynccontextmanager
        async def _ctx() -> Any:
            async with self._session_factory() as session:
                try:
                    yield session
                    await session.commit()
                except Exception:
                    await session.rollback()
                    raise

        return _ctx()

    async def dispose(self) -> None:
        await self._engine.dispose()


class FakeExecutor:
    """Segment executor returning a completed segment per call."""

    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, *, mission: Any, step: Any, prompt: str, attempt: int) -> SegmentResult:
        self.calls += 1
        return SegmentResult(
            reason="completed",
            text=f"step {step.step_no} done",
            usage={"total_tokens": 10, "cost_usd": 0.01},
        )


class CrashingPlanner:
    """Planner whose ``plan`` raises — exercises the ``_drive`` crash guard."""

    async def plan(self, mission: Any) -> MissionPlan:
        raise RuntimeError("boom")


def _two_step_plan() -> MissionPlan:
    return MissionPlan(
        goal="g",
        expected_output="",
        steps=[
            MissionStepState(step_no=1, description="first"),
            MissionStepState(step_no=2, description="second", depends_on=[1]),
        ],
    )


@pytest.mark.asyncio
async def test_ensure_running_drives_mission_to_completion() -> None:
    db = _FakeDb()
    await db.create_all()
    try:
        planner = StaticPlanner(_two_step_plan())
        executor = FakeExecutor()
        svc = MissionService(
            db=db,
            planner_factory=lambda m: planner,
            executor_factory=lambda m: executor,
            verifier_factory=lambda m: AlwaysPassVerifier(),
        )
        mission = Mission(user_id=uuid.uuid4(), goal="g")
        mission = await svc.store.create(mission)

        assert await svc.ensure_running(mission) is True
        # Idempotent while active.
        assert await svc.ensure_running(mission) is False

        final = await _wait_terminal(svc, mission.id)
        assert final is not None and final.status == MissionStatus.COMPLETED
        steps = await svc.store.list_steps(mission.id)
        assert len(steps) == 2
        assert all(s.status == MissionStepStatus.PASSED for s in steps)
        assert executor.calls == 2
        assert not svc.is_running(mission.id)
    finally:
        await db.dispose()


@pytest.mark.asyncio
async def test_runner_crash_marks_mission_failed() -> None:
    """An exception outside the runner's internal guards must not strand the mission."""
    db = _FakeDb()
    await db.create_all()
    try:
        svc = MissionService(
            db=db,
            planner_factory=lambda m: CrashingPlanner(),
            executor_factory=lambda m: FakeExecutor(),
            verifier_factory=lambda m: AlwaysPassVerifier(),
        )
        mission = Mission(user_id=uuid.uuid4(), goal="g")
        mission = await svc.store.create(mission)
        await svc.ensure_running(mission)

        final = await _wait_terminal(svc, mission.id)
        assert final is not None and final.status == MissionStatus.FAILED
        assert "boom" in (final.error or "")
    finally:
        await db.dispose()


@pytest.mark.asyncio
async def test_pause_sets_paused_status() -> None:
    db = _FakeDb()
    await db.create_all()
    try:
        planner = StaticPlanner(_two_step_plan())
        svc = MissionService(
            db=db,
            planner_factory=lambda m: planner,
            executor_factory=lambda m: FakeExecutor(),
            verifier_factory=lambda m: AlwaysPassVerifier(),
        )
        mission = Mission(user_id=uuid.uuid4(), goal="g")
        mission = await svc.store.create(mission)
        # Pause applies to READY/RUNNING missions only; a fresh PLANNING row stays put.
        from StAgent.db.models.mission import MissionStatus as _MS

        await svc.store.update_status(mission.id, _MS.RUNNING)
        paused = await svc.pause(mission.id)
        assert paused is not None and paused.status == MissionStatus.PAUSED
    finally:
        await db.dispose()


async def _wait_terminal(svc: MissionService, mission_id: uuid.UUID) -> Mission | None:
    for _ in range(200):
        mission = await svc.store.get(mission_id)
        if mission is None:
            return None
        if mission.status in (
            MissionStatus.COMPLETED,
            MissionStatus.FAILED,
            MissionStatus.ABORTED,
            MissionStatus.EXPIRED,
        ):
            return mission
        await asyncio.sleep(0.02)
    return await svc.store.get(mission_id)
