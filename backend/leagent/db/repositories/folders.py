"""Folder persistence repository."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, cast

from sqlalchemy import update as sa_update
from sqlmodel import col, select

from leagent.db.models.base import utc_now
from leagent.db.models.folder import Folder
from leagent.db.sqlite_compat import load_entity_by_id, uuid_column_equals

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.engine import CursorResult

    from leagent.db.service import DatabaseService


class FolderRepository(Protocol):
    """Protocol for folder persistence."""

    async def get(self, folder_id: UUID) -> Folder | None:
        """Return a folder by id (including soft-deleted)."""
        ...

    async def list_for_user(
        self,
        user_id: UUID,
        *,
        parent_id: UUID | None = None,
        offset: int = 0,
        limit: int = 100,
    ) -> list[Folder]:
        """Return non-deleted folders owned by *user_id*."""
        ...

    async def create(self, folder: Folder) -> Folder:
        """Persist a new folder row."""
        ...

    async def update(self, folder: Folder) -> Folder:
        """Update an existing folder row."""
        ...

    async def soft_delete(self, folder_id: UUID, user_id: UUID) -> bool:
        """Soft-delete a folder owned by *user_id*; return whether it changed."""
        ...

    async def search_by_name(
        self,
        user_id: UUID,
        query: str,
    ) -> list[Folder]:
        """Return non-deleted folders whose name contains *query*."""
        ...


class DbFolderRepository:
    """``DatabaseService``-backed :class:`FolderRepository`."""

    def __init__(self, db: DatabaseService) -> None:
        self._db = db

    async def get(self, folder_id: UUID) -> Folder | None:
        async with self._db.session() as session:
            return await load_entity_by_id(
                session, Folder, folder_id, parent_table="folders"
            )

    async def list_for_user(
        self,
        user_id: UUID,
        *,
        parent_id: UUID | None = None,
        offset: int = 0,
        limit: int = 100,
    ) -> list[Folder]:
        async with self._db.session() as session:
            stmt = (
                select(Folder)
                .where(uuid_column_equals(session, Folder.user_id, user_id))
                .where(col(Folder.is_deleted).is_(False))
            )
            if parent_id is not None:
                stmt = stmt.where(
                    uuid_column_equals(session, Folder.parent_id, parent_id)
                )
            stmt = stmt.order_by(col(Folder.position), col(Folder.created_at)).offset(offset).limit(limit)
            result = await session.exec(stmt)
            return list(result.all())

    async def create(self, folder: Folder) -> Folder:
        async with self._db.session() as session:
            session.add(folder)
            await session.flush()
            await session.refresh(folder)
            return folder

    async def update(self, folder: Folder) -> Folder:
        async with self._db.session() as session:
            session.add(folder)
            await session.flush()
            await session.refresh(folder)
            return folder

    async def soft_delete(self, folder_id: UUID, user_id: UUID) -> bool:
        async with self._db.session() as session:
            now = utc_now()
            result = await session.execute(
                sa_update(Folder)
                .where(uuid_column_equals(session, Folder.id, folder_id))
                .where(uuid_column_equals(session, Folder.user_id, user_id))
                .where(col(Folder.is_deleted).is_(False))
                .values(
                    is_deleted=True,
                    deleted_at=now,
                    updated_at=now,
                )
            )
            return bool(cast("CursorResult[Any]", result).rowcount)

    async def search_by_name(
        self,
        user_id: UUID,
        query: str,
    ) -> list[Folder]:
        async with self._db.session() as session:
            stmt = (
                select(Folder)
                .where(col(Folder.is_deleted).is_(False))
                .where(col(Folder.name).contains(query))
            )
            stmt = stmt.where(uuid_column_equals(session, Folder.user_id, user_id))
            result = await session.exec(stmt)
            return list(result.all())
