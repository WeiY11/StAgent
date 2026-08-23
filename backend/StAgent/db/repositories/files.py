"""File persistence repository."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, cast

from sqlalchemy import update as sa_update
from sqlmodel import col, select

from StAgent.db.models.base import utc_now
from StAgent.db.models.file import File
from StAgent.db.sqlite_compat import (
    load_entity_by_id,
    same_user_id,
    uuid_column_equals,
    uuid_column_in,
)

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.engine import CursorResult

    from StAgent.db.service import DatabaseService


class FileRepository(Protocol):
    """Protocol for file persistence."""

    async def get(self, file_id: UUID) -> File | None:
        """Return a file row by id (including soft-deleted)."""
        ...

    async def get_for_user(self, file_id: UUID, user_id: UUID) -> File | None:
        """Return a non-deleted file owned by *user_id*."""
        ...

    async def list_for_session(self, session_id: UUID) -> list[File]:
        """Return non-deleted files attached to *session_id*."""
        ...

    async def get_many_for_user(
        self, file_ids: list[UUID], user_id: UUID
    ) -> list[File]:
        """Return non-deleted files owned by *user_id* matching *file_ids*."""
        ...

    async def soft_delete(self, file_id: UUID, user_id: UUID) -> bool:
        """Soft-delete a file owned by *user_id*; return whether it changed."""
        ...

    async def list_for_folder(
        self, folder_id: UUID, *, user_id: UUID | None = None
    ) -> list[File]:
        """Return non-deleted files in *folder_id*, optionally filtered by owner."""
        ...

    async def update(self, file: File) -> File:
        """Persist changes to an existing file row."""
        ...


class DbFileRepository:
    """``DatabaseService``-backed :class:`FileRepository`."""

    def __init__(self, db: DatabaseService) -> None:
        self._db = db

    async def get(self, file_id: UUID) -> File | None:
        async with self._db.session() as session:
            return await load_entity_by_id(session, File, file_id, parent_table="files")

    async def get_for_user(self, file_id: UUID, user_id: UUID) -> File | None:
        async with self._db.session() as session:
            row = await load_entity_by_id(session, File, file_id, parent_table="files")
            if row is None or row.is_deleted or not same_user_id(row.user_id, user_id):
                return None
            return row

    async def list_for_session(self, session_id: UUID) -> list[File]:
        async with self._db.session() as session:
            result = await session.exec(
                select(File)
                .where(uuid_column_equals(session, File.session_id, session_id))
                .where(col(File.is_deleted).is_(False))
                .order_by(col(File.created_at))
            )
            return list(result.all())

    async def get_many_for_user(
        self, file_ids: list[UUID], user_id: UUID
    ) -> list[File]:
        if not file_ids:
            return []
        async with self._db.session() as session:
            result = await session.exec(
                select(File)
                .where(uuid_column_in(session, File.id, file_ids))
                .where(uuid_column_equals(session, File.user_id, user_id))
                .where(col(File.is_deleted).is_(False))
            )
            return list(result.all())

    async def soft_delete(self, file_id: UUID, user_id: UUID) -> bool:
        async with self._db.session() as session:
            now = utc_now()
            result = await session.execute(
                sa_update(File)
                .where(uuid_column_equals(session, File.id, file_id))
                .where(uuid_column_equals(session, File.user_id, user_id))
                .where(col(File.is_deleted).is_(False))
                .values(
                    is_deleted=True,
                    deleted_at=now,
                    updated_at=now,
                )
            )
            return bool(cast("CursorResult[Any]", result).rowcount)

    async def list_for_folder(
        self, folder_id: UUID, *, user_id: UUID | None = None
    ) -> list[File]:
        async with self._db.session() as session:
            stmt = (
                select(File)
                .where(uuid_column_equals(session, File.folder_id, folder_id))
                .where(col(File.is_deleted).is_(False))
            )
            if user_id is not None:
                stmt = stmt.where(uuid_column_equals(session, File.user_id, user_id))
            result = await session.exec(stmt)
            return list(result.all())

    async def update(self, file: File) -> File:
        async with self._db.session() as session:
            session.add(file)
            await session.flush()
            await session.refresh(file)
            return file
