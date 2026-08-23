"""Mission persistence repository (mission + step working state).

Modeled on :class:`StAgent.db.repositories.tasks.DbTaskRepository`: a
``Protocol`` plus a ``DatabaseService``-backed implementation, with the
SQLite UUID-compat helpers used wherever IDs are compared.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, cast

from sqlalchemy import delete as sa_delete
from sqlalchemy import update as sa_update
from sqlmodel import col, select

from StAgent.db.models.mission import Mission, MissionStatus, MissionStep
from StAgent.db.sqlite_compat import load_entity_by_id, uuid_column_equals

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.engine import CursorResult

    from StAgent.db.service import DatabaseService


class MissionRepository(Protocol):
    """Protocol for mission persistence."""

    async def get(self, mission_id: UUID) -> Mission | None:
        """Return the mission row for *mission_id*."""
        ...

    async def get_with_steps(self, mission_id: UUID) -> tuple[Mission, list[MissionStep]] | None:
        """Return the mission row and its steps (ordered by ``step_no``)."""
        ...

    async def create(self, mission: Mission) -> Mission:
        """Persist a new mission row."""
        ...

    async def update(self, mission: Mission) -> Mission:
        """Persist changes to an existing mission row."""
        ...

    async def update_status(
        self,
        mission_id: UUID,
        status: MissionStatus,
        *,
        pause_reason: str | None = None,
        error: str | None = None,
    ) -> bool:
        """Update a mission's status; return whether the row existed."""
        ...

    async def list_for_user(
        self, user_id: UUID, *, status: MissionStatus | None = None, limit: int = 100
    ) -> list[Mission]:
        """Return missions owned by *user_id*, newest first."""
        ...

    async def delete(self, mission_id: UUID) -> None:
        """Delete the mission and its steps."""
        ...

    async def list_steps(self, mission_id: UUID, *, plan_id: int | None = None) -> list[MissionStep]:
        """Return steps for *mission_id*, ordered by ``step_no``."""
        ...

    async def get_step(self, mission_id: UUID, step_no: int, *, plan_id: int | None = None) -> MissionStep | None:
        """Return the step with *step_no* (optionally scoped to *plan_id*)."""
        ...

    async def upsert_step(self, step: MissionStep) -> MissionStep:
        """Insert or replace the step identified by mission/plan/step_no."""
        ...

    async def replace_steps(self, mission_id: UUID, plan_id: int, steps: list[MissionStep]) -> list[MissionStep]:
        """Replace every step of *plan_id* with *steps* (replan)."""
        ...


class DbMissionRepository:
    """``DatabaseService``-backed :class:`MissionRepository`."""

    def __init__(self, db: DatabaseService) -> None:
        self._db = db

    async def get(self, mission_id: UUID) -> Mission | None:
        async with self._db.session() as session:
            return await load_entity_by_id(session, Mission, mission_id, parent_table="agent_missions")

    async def get_with_steps(self, mission_id: UUID) -> tuple[Mission, list[MissionStep]] | None:
        async with self._db.session() as session:
            mission = await load_entity_by_id(session, Mission, mission_id, parent_table="agent_missions")
            if mission is None:
                return None
            steps = await self.list_steps(mission_id)
            return mission, steps

    async def create(self, mission: Mission) -> Mission:
        async with self._db.session() as session:
            session.add(mission)
            await session.flush()
            await session.refresh(mission)
            return mission

    async def update(self, mission: Mission) -> Mission:
        async with self._db.session() as session:
            session.add(mission)
            await session.flush()
            await session.refresh(mission)
            return mission

    async def update_status(
        self,
        mission_id: UUID,
        status: MissionStatus,
        *,
        pause_reason: str | None = None,
        error: str | None = None,
    ) -> bool:
        async with self._db.session() as session:
            values: dict[str, Any] = {"status": status}
            if pause_reason is not None:
                values["pause_reason"] = pause_reason
            if error is not None:
                values["error"] = error
            result = await session.execute(
                sa_update(Mission).where(uuid_column_equals(session, Mission.id, mission_id)).values(**values)
            )
            return bool(cast("CursorResult[Any]", result).rowcount)

    async def list_for_user(
        self, user_id: UUID, *, status: MissionStatus | None = None, limit: int = 100
    ) -> list[Mission]:
        async with self._db.session() as session:
            stmt = select(Mission).where(uuid_column_equals(session, Mission.user_id, user_id))
            if status is not None:
                stmt = stmt.where(Mission.status == status)
            stmt = stmt.order_by(col(Mission.created_at).desc()).limit(limit)
            result = await session.exec(stmt)
            return list(result.all())

    async def delete(self, mission_id: UUID) -> None:
        async with self._db.session() as session:
            await session.execute(
                sa_delete(MissionStep).where(uuid_column_equals(session, MissionStep.mission_id, mission_id))
            )
            mission = await load_entity_by_id(session, Mission, mission_id, parent_table="agent_missions")
            if mission is not None:
                await session.delete(mission)

    async def list_steps(self, mission_id: UUID, *, plan_id: int | None = None) -> list[MissionStep]:
        async with self._db.session() as session:
            stmt = select(MissionStep).where(uuid_column_equals(session, MissionStep.mission_id, mission_id))
            if plan_id is not None:
                stmt = stmt.where(col(MissionStep.plan_id) == plan_id)
            stmt = stmt.order_by(col(MissionStep.step_no))
            result = await session.exec(stmt)
            return list(result.all())

    async def get_step(self, mission_id: UUID, step_no: int, *, plan_id: int | None = None) -> MissionStep | None:
        async with self._db.session() as session:
            stmt = select(MissionStep).where(
                uuid_column_equals(session, MissionStep.mission_id, mission_id),
                col(MissionStep.step_no) == step_no,
            )
            if plan_id is not None:
                stmt = stmt.where(col(MissionStep.plan_id) == plan_id)
            result = await session.exec(stmt)
            return result.first()

    async def upsert_step(self, step: MissionStep) -> MissionStep:
        async with self._db.session() as session:
            stmt = select(MissionStep).where(
                uuid_column_equals(session, MissionStep.mission_id, step.mission_id),
                col(MissionStep.plan_id) == step.plan_id,
                col(MissionStep.step_no) == step.step_no,
            )
            result = await session.exec(stmt)
            row = result.first()
            if row is None:
                row = step
            else:
                row.description = step.description
                row.kind = step.kind
                row.depends_on = step.depends_on
                row.wave = step.wave
                row.status = step.status
                row.attempt = step.attempt
                row.verify_result = step.verify_result
                row.segment_summary = step.segment_summary
                row.checkpoint_id = step.checkpoint_id
                row.started_at = step.started_at
                row.completed_at = step.completed_at
            session.add(row)
            await session.flush()
            await session.refresh(row)
            return row

    async def replace_steps(self, mission_id: UUID, plan_id: int, steps: list[MissionStep]) -> list[MissionStep]:
        async with self._db.session() as session:
            await session.execute(
                sa_delete(MissionStep).where(
                    uuid_column_equals(session, MissionStep.mission_id, mission_id),
                    col(MissionStep.plan_id) == plan_id,
                )
            )
            for step in steps:
                session.add(step)
            await session.flush()
            return steps


__all__ = ["MissionRepository", "DbMissionRepository"]
