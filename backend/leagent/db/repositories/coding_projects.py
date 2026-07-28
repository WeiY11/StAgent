"""Coding-project persistence repository."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, cast

from sqlalchemy import update as sa_update
from sqlmodel import col, select

from leagent.db.models.base import utc_now
from leagent.db.models.coding_project import CodingProject, CodingProjectStatus
from leagent.db.sqlite_compat import load_entity_by_id, uuid_column_equals

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.engine import CursorResult

    from leagent.db.service import DatabaseService


class CodingProjectRepository(Protocol):
    """Protocol for coding-project persistence."""

    async def get(self, project_id: UUID) -> CodingProject | None:
        """Return a coding project by id (including soft-deleted)."""
        ...

    async def list_for_user(
        self,
        user_id: UUID,
        *,
        status: CodingProjectStatus | None = None,
        offset: int = 0,
        limit: int = 100,
    ) -> list[CodingProject]:
        """Return non-deleted coding projects owned by *user_id*."""
        ...

    async def create(self, project: CodingProject) -> CodingProject:
        """Persist a new coding project row."""
        ...

    async def update(self, project: CodingProject) -> CodingProject:
        """Update an existing coding project row."""
        ...

    async def soft_delete(self, project_id: UUID, user_id: UUID) -> bool:
        """Soft-delete a coding project owned by *user_id*; return whether it changed."""
        ...


class DbCodingProjectRepository:
    """``DatabaseService``-backed :class:`CodingProjectRepository`."""

    def __init__(self, db: DatabaseService) -> None:
        self._db = db

    async def get(self, project_id: UUID) -> CodingProject | None:
        async with self._db.session() as session:
            return await load_entity_by_id(
                session, CodingProject, project_id, parent_table="coding_projects"
            )

    async def list_for_user(
        self,
        user_id: UUID,
        *,
        status: CodingProjectStatus | None = None,
        offset: int = 0,
        limit: int = 100,
    ) -> list[CodingProject]:
        async with self._db.session() as session:
            stmt = (
                select(CodingProject)
                .where(uuid_column_equals(session, CodingProject.user_id, user_id))
                .where(col(CodingProject.is_deleted).is_(False))
            )
            if status is not None:
                stmt = stmt.where(CodingProject.status == status)
            stmt = stmt.order_by(col(CodingProject.created_at).desc()).offset(offset).limit(limit)
            result = await session.exec(stmt)
            return list(result.all())

    async def create(self, project: CodingProject) -> CodingProject:
        async with self._db.session() as session:
            session.add(project)
            await session.flush()
            await session.refresh(project)
            return project

    async def update(self, project: CodingProject) -> CodingProject:
        async with self._db.session() as session:
            session.add(project)
            await session.flush()
            await session.refresh(project)
            return project

    async def soft_delete(self, project_id: UUID, user_id: UUID) -> bool:
        async with self._db.session() as session:
            now = utc_now()
            result = await session.execute(
                sa_update(CodingProject)
                .where(uuid_column_equals(session, CodingProject.id, project_id))
                .where(uuid_column_equals(session, CodingProject.user_id, user_id))
                .where(col(CodingProject.is_deleted).is_(False))
                .values(
                    is_deleted=True,
                    deleted_at=now,
                    updated_at=now,
                )
            )
            return bool(cast("CursorResult[Any]", result).rowcount)
