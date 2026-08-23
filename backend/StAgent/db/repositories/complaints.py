"""Complaint ticket persistence repository."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, cast

from sqlalchemy import func
from sqlalchemy import update as sa_update
from sqlmodel import col, select

from StAgent.db.models.base import utc_now
from StAgent.db.models.complaint import Complaint
from StAgent.db.sqlite_compat import load_entity_by_id, uuid_column_equals

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.engine import CursorResult

    from StAgent.db.service import DatabaseService


class ComplaintRepository(Protocol):
    """Protocol for complaint persistence."""

    async def get(self, complaint_id: UUID) -> Complaint | None:
        """Return a complaint by id (including soft-deleted)."""
        ...

    async def list_for_user(
        self,
        user_id: UUID,
        *,
        status: str | None = None,
        category: str | None = None,
        severity: str | None = None,
        limit: int = 100,
    ) -> list[Complaint]:
        """Return non-deleted complaints owned by *user_id*, newest first."""
        ...

    async def create(self, complaint: Complaint) -> Complaint:
        """Persist a new complaint row."""
        ...

    async def update(self, complaint: Complaint) -> Complaint:
        """Persist changes to an existing complaint row."""
        ...

    async def soft_delete(self, complaint_id: UUID, user_id: UUID) -> bool:
        """Soft-delete a complaint owned by *user_id*; return whether it changed."""
        ...

    async def count_by_status(self, user_id: UUID) -> dict[str, int]:
        """Return ``{status: count}`` for non-deleted complaints of *user_id*."""
        ...


class DbComplaintRepository:
    """``DatabaseService``-backed :class:`ComplaintRepository`."""

    def __init__(self, db: DatabaseService) -> None:
        self._db = db

    async def get(self, complaint_id: UUID) -> Complaint | None:
        async with self._db.session() as session:
            return await load_entity_by_id(
                session, Complaint, complaint_id, parent_table="complaints"
            )

    async def list_for_user(
        self,
        user_id: UUID,
        *,
        status: str | None = None,
        category: str | None = None,
        severity: str | None = None,
        limit: int = 100,
    ) -> list[Complaint]:
        async with self._db.session() as session:
            stmt = (
                select(Complaint)
                .where(uuid_column_equals(session, Complaint.user_id, user_id))
                .where(col(Complaint.is_deleted).is_(False))
            )
            if status is not None:
                stmt = stmt.where(Complaint.status == status)
            if category is not None:
                stmt = stmt.where(Complaint.category == category)
            if severity is not None:
                stmt = stmt.where(Complaint.severity == severity)
            stmt = stmt.order_by(col(Complaint.created_at).desc()).limit(limit)
            result = await session.exec(stmt)
            return list(result.all())

    async def create(self, complaint: Complaint) -> Complaint:
        async with self._db.session() as session:
            session.add(complaint)
            await session.flush()
            await session.refresh(complaint)
            return complaint

    async def update(self, complaint: Complaint) -> Complaint:
        async with self._db.session() as session:
            session.add(complaint)
            await session.flush()
            await session.refresh(complaint)
            return complaint

    async def soft_delete(self, complaint_id: UUID, user_id: UUID) -> bool:
        async with self._db.session() as session:
            now = utc_now()
            result = await session.execute(
                sa_update(Complaint)
                .where(uuid_column_equals(session, Complaint.id, complaint_id))
                .where(uuid_column_equals(session, Complaint.user_id, user_id))
                .where(col(Complaint.is_deleted).is_(False))
                .values(
                    is_deleted=True,
                    deleted_at=now,
                    updated_at=now,
                )
            )
            return bool(cast("CursorResult[Any]", result).rowcount)

    async def count_by_status(self, user_id: UUID) -> dict[str, int]:
        async with self._db.session() as session:
            stmt = (
                select(Complaint.status, func.count())
                .where(uuid_column_equals(session, Complaint.user_id, user_id))
                .where(col(Complaint.is_deleted).is_(False))
                .group_by(col(Complaint.status))
            )
            result = await session.exec(stmt)
            return {status: count for status, count in result.all()}
