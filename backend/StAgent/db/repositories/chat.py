"""Chat persistence repository (sessions + messages)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol
from uuid import UUID

from sqlmodel import col, select

from StAgent.db.models.message import ChatSession, Message
from StAgent.db.sqlite_compat import load_entity_by_id, same_user_id, uuid_column_equals

if TYPE_CHECKING:
    from StAgent.db.service import DatabaseService


class ChatRepository(Protocol):
    """Protocol for chat session/message persistence."""

    async def get_session(self, session_id: UUID) -> ChatSession | None:
        """Return a chat session by id."""
        ...

    async def get_session_for_user(
        self, session_id: UUID, user_id: UUID
    ) -> ChatSession | None:
        """Return a chat session owned by *user_id*."""
        ...

    async def list_sessions_for_user(
        self, user_id: UUID, *, offset: int = 0, limit: int = 50
    ) -> list[ChatSession]:
        """Return active chat sessions owned by *user_id*."""
        ...

    async def list_messages(
        self, session_id: UUID, *, limit: int = 200
    ) -> list[Message]:
        """Return messages for *session_id* in chronological order."""
        ...


class DbChatRepository:
    """``DatabaseService``-backed :class:`ChatRepository`."""

    def __init__(self, db: DatabaseService) -> None:
        self._db = db

    async def get_session(self, session_id: UUID) -> ChatSession | None:
        async with self._db.session() as session:
            return await load_entity_by_id(
                session,
                ChatSession,
                session_id,
                parent_table="chat_sessions",
            )

    async def get_session_for_user(
        self, session_id: UUID, user_id: UUID
    ) -> ChatSession | None:
        async with self._db.session() as session:
            row = await load_entity_by_id(
                session,
                ChatSession,
                session_id,
                parent_table="chat_sessions",
            )
            if row is None or not same_user_id(row.user_id, user_id):
                return None
            return row

    async def list_sessions_for_user(
        self, user_id: UUID, *, offset: int = 0, limit: int = 50
    ) -> list[ChatSession]:
        async with self._db.session() as session:
            result = await session.exec(
                select(ChatSession)
                .where(uuid_column_equals(session, ChatSession.user_id, user_id))
                .where(ChatSession.is_active == True)  # noqa: E712
                .order_by(col(ChatSession.last_message_at).desc())
                .offset(offset)
                .limit(limit)
            )
            return list(result.all())

    async def list_messages(
        self, session_id: UUID, *, limit: int = 200
    ) -> list[Message]:
        async with self._db.session() as session:
            result = await session.exec(
                select(Message)
                .where(uuid_column_equals(session, Message.session_id, session_id))
                .order_by(col(Message.created_at), col(Message.id))
                .limit(limit)
            )
            return list(result.all())
