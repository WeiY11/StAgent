"""Flow persistence repository."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, cast

from sqlalchemy import update as sa_update
from sqlmodel import col, select

from StAgent.db.models.base import utc_now
from StAgent.db.models.flow import Flow, FlowStatus, FlowType
from StAgent.db.sqlite_compat import load_entity_by_id, uuid_column_equals

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.engine import CursorResult

    from StAgent.db.service import DatabaseService


class FlowRepository(Protocol):
    """Protocol for flow persistence."""

    async def get(self, flow_id: UUID) -> Flow | None:
        """Return a flow by id (including soft-deleted)."""
        ...

    async def list_for_user(
        self,
        user_id: UUID,
        *,
        status: FlowStatus | None = None,
        flow_type: FlowType | None = None,
        offset: int = 0,
        limit: int = 100,
    ) -> list[Flow]:
        """Return non-deleted flows owned by *user_id*."""
        ...

    async def create(self, flow: Flow) -> Flow:
        """Persist a new flow row."""
        ...

    async def update(self, flow: Flow) -> Flow:
        """Update an existing flow row."""
        ...

    async def soft_delete(self, flow_id: UUID, user_id: UUID) -> bool:
        """Soft-delete a flow owned by *user_id*; return whether it changed."""
        ...


class DbFlowRepository:
    """``DatabaseService``-backed :class:`FlowRepository`."""

    def __init__(self, db: DatabaseService) -> None:
        self._db = db

    async def get(self, flow_id: UUID) -> Flow | None:
        async with self._db.session() as session:
            return await load_entity_by_id(
                session, Flow, flow_id, parent_table="flows"
            )

    async def list_for_user(
        self,
        user_id: UUID,
        *,
        status: FlowStatus | None = None,
        flow_type: FlowType | None = None,
        offset: int = 0,
        limit: int = 100,
    ) -> list[Flow]:
        async with self._db.session() as session:
            stmt = (
                select(Flow)
                .where(uuid_column_equals(session, Flow.user_id, user_id))
                .where(col(Flow.is_deleted).is_(False))
            )
            if status is not None:
                stmt = stmt.where(Flow.status == status)
            if flow_type is not None:
                stmt = stmt.where(Flow.flow_type == flow_type)
            stmt = stmt.order_by(col(Flow.created_at).desc()).offset(offset).limit(limit)
            result = await session.exec(stmt)
            return list(result.all())

    async def create(self, flow: Flow) -> Flow:
        async with self._db.session() as session:
            session.add(flow)
            await session.flush()
            await session.refresh(flow)
            return flow

    async def update(self, flow: Flow) -> Flow:
        async with self._db.session() as session:
            session.add(flow)
            await session.flush()
            await session.refresh(flow)
            return flow

    async def soft_delete(self, flow_id: UUID, user_id: UUID) -> bool:
        async with self._db.session() as session:
            now = utc_now()
            result = await session.execute(
                sa_update(Flow)
                .where(uuid_column_equals(session, Flow.id, flow_id))
                .where(uuid_column_equals(session, Flow.user_id, user_id))
                .where(col(Flow.is_deleted).is_(False))
                .values(
                    is_deleted=True,
                    deleted_at=now,
                    updated_at=now,
                )
            )
            return bool(cast("CursorResult[Any]", result).rowcount)
