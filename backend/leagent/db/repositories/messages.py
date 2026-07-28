"""Message persistence repository."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, cast

from sqlalchemy import update as sa_update
from sqlmodel import col, select

from leagent.db.models.message import Message, MessageStatus
from leagent.db.sqlite_compat import load_entity_by_id, uuid_column_equals

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.engine import CursorResult

    from leagent.db.service import DatabaseService


class MessageRepository(Protocol):
    """Protocol for message persistence."""

    async def get(self, message_id: UUID) -> Message | None:
        """Return a message by id."""
        ...

    async def list_for_session(
        self,
        session_id: UUID,
        *,
        limit: int = 200,
    ) -> list[Message]:
        """Return messages for *session_id* in chronological order."""
        ...

    async def create(self, message: Message) -> Message:
        """Persist a new message row."""
        ...

    async def update_status(
        self, message_id: UUID, status: MessageStatus
    ) -> bool:
        """Update a message's status; return whether the row existed."""
        ...

    async def delete(self, message_id: UUID) -> bool:
        """Hard-delete a message; return whether the row existed."""
        ...


class DbMessageRepository:
    """``DatabaseService``-backed :class:`MessageRepository`."""

    def __init__(self, db: DatabaseService) -> None:
        self._db = db

    async def get(self, message_id: UUID) -> Message | None:
        async with self._db.session() as session:
            return await load_entity_by_id(
                session, Message, message_id, parent_table="messages"
            )

    async def list_for_session(
        self,
        session_id: UUID,
        *,
        limit: int = 200,
    ) -> list[Message]:
        async with self._db.session() as session:
            result = await session.exec(
                select(Message)
                .where(uuid_column_equals(session, Message.session_id, session_id))
                .order_by(col(Message.created_at), col(Message.id))
                .limit(limit)
            )
            return list(result.all())

    async def create(self, message: Message) -> Message:
        async with self._db.session() as session:
            session.add(message)
            await session.flush()
            await session.refresh(message)
            return message

    async def update_status(
        self, message_id: UUID, status: MessageStatus
    ) -> bool:
        async with self._db.session() as session:
            result = await session.execute(
                sa_update(Message)
                .where(uuid_column_equals(session, Message.id, message_id))
                .values(status=status)
            )
            return bool(cast("CursorResult[Any]", result).rowcount)

    async def delete(self, message_id: UUID) -> bool:
        async with self._db.session() as session:
            row = await load_entity_by_id(
                session, Message, message_id, parent_table="messages"
            )
            if row is None:
                return False
            await session.delete(row)
            return True
