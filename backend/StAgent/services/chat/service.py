"""Chat service for session and message management."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from pydantic import ValidationError
from sqlalchemy import and_, delete, text
from sqlalchemy import update as sa_update
from sqlmodel import col, select

from StAgent.db.models.agent_memory import AgentEpisode
from StAgent.db.models.base import naive_utc_for_db_column, utc_now
from StAgent.db.models.canvas import CanvasDocument
from StAgent.db.models.file import File
from StAgent.db.models.message import (
    AUTHORIZED_ROOTS_META_KEY,
    ChatSession,
    Message,
    MessageRead,
    MessageRole,
    MessageStatus,
    SessionRead,
    chat_session_to_read,
    parse_authorized_roots_from_session_metadata,
)
from StAgent.db.sqlite_compat import (
    load_chat_session_by_id,
    parse_uuid_stored,
    same_user_id,
    session_dialect_name,
    sqlite_insert_message,
    sqlite_parent_id_text,
    uuid_column_equals,
    uuid_column_in,
)
from StAgent.services.base import Service, ServiceType, service_factory
from StAgent.services.cache import build_scoped_cache_key

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlmodel.ext.asyncio.session import AsyncSession

    from StAgent.config.settings import Settings
    from StAgent.db.service import DatabaseService
    from StAgent.services.cache.service import CacheService

logger = logging.getLogger(__name__)

TOKENS_PER_CHAR = 0.25
CACHE_SESSION_TTL = 3600
CACHE_MESSAGES_TTL = 300


def estimate_tokens(text: str) -> int:
    """Estimate token count for text (approximation)."""
    return int(len(text) * TOKENS_PER_CHAR)


async def _message_session_sid_eq(db: AsyncSession, session_id: UUID) -> Any:
    """Return session filter expression for ``Message`` rows."""
    if session_dialect_name(db) == "sqlite":
        return uuid_column_equals(db, Message.session_id, session_id)
    return Message.session_id == session_id


def _result_rowcount(result: Any) -> int:
    return int(getattr(result, "rowcount", 0) or 0)


async def _sqlite_update_chat_session(
    db: AsyncSession,
    session_id: UUID,
    values: dict[str, Any],
) -> None:
    """Update non-FK ``chat_sessions`` fields with UUID-normalized PK matching."""
    if not values:
        return
    await db.execute(
        sa_update(ChatSession)
        .where(uuid_column_equals(db, ChatSession.id, session_id))
        .values(**values)
    )


async def _sqlite_update_message(
    db: AsyncSession,
    message_id: UUID,
    values: dict[str, Any],
) -> None:
    """Update non-FK ``messages`` fields with UUID-normalized PK matching."""
    if not values:
        return
    await db.execute(
        sa_update(Message)
        .where(uuid_column_equals(db, Message.id, message_id))
        .values(**values)
    )


async def _delete_hard_session_dependents(db: AsyncSession, session_id: UUID) -> tuple[int, int, int, int]:
    """Delete rows that FK to ``chat_sessions`` before removing the session itself.

    Returns deleted row counts ``(canvas_documents, agent_episodes, files, messages)``.
    """
    if session_dialect_name(db) == "sqlite":
        canvas_r = await db.execute(
            delete(CanvasDocument).where(
                uuid_column_equals(db, CanvasDocument.session_id, session_id)
            ),
        )
        ep_r = await db.execute(
            delete(AgentEpisode).where(
                uuid_column_equals(db, AgentEpisode.session_id, session_id)
            ),
        )
        file_r = await db.execute(
            delete(File).where(
                and_(
                    col(File.session_id).is_not(None),
                    uuid_column_equals(db, File.session_id, session_id),
                ),
            ),
        )
        msg_r = await db.execute(
            delete(Message).where(uuid_column_equals(db, Message.session_id, session_id))
        )
    else:
        canvas_r = await db.execute(delete(CanvasDocument).where(col(CanvasDocument.session_id) == session_id))
        ep_r = await db.execute(delete(AgentEpisode).where(col(AgentEpisode.session_id) == session_id))
        file_r = await db.execute(delete(File).where(col(File.session_id) == session_id))
        msg_r = await db.execute(delete(Message).where(col(Message.session_id) == session_id))
    return (
        _result_rowcount(canvas_r),
        _result_rowcount(ep_r),
        _result_rowcount(file_r),
        _result_rowcount(msg_r),
    )


@service_factory(ServiceType.CHAT)
class ChatService(Service):
    """Service for managing chat sessions and messages.

    Features:
    - Session creation and management
    - Message storage with full metadata
    - History retrieval with pagination
    - Token counting and estimation
    - Cache-backed session lookups
    """

    def __init__(
        self,
        settings: Settings,
        db_service: DatabaseService | None = None,
        cache_service: CacheService | None = None,
    ) -> None:
        super().__init__(settings)
        self._db = db_service
        self._cache = cache_service

    @property
    def name(self) -> str:
        return "ChatService"

    def set_dependencies(
        self,
        db_service: DatabaseService,
        cache_service: CacheService | None = None,
    ) -> None:
        """Set service dependencies after initialization."""
        self._db = db_service
        self._cache = cache_service

    async def _do_health_check(self) -> dict[str, Any]:
        return {
            "db_connected": self._db is not None,
            "cache_connected": self._cache is not None,
        }

    async def _max_message_created_at(
        self,
        db: AsyncSession,
        session_id: UUID,
    ) -> datetime | None:
        """Latest ``created_at`` for rows in *session_id* (may be ``None``)."""
        from sqlmodel import func as sqlfunc

        sid_eq = await _message_session_sid_eq(db, session_id)
        stmt = select(sqlfunc.max(Message.created_at)).where(sid_eq)
        result = await db.execute(stmt)
        return result.scalar_one_or_none()

    async def _next_message_created_at(
        self,
        db: AsyncSession,
        session_id: UUID,
    ) -> datetime:
        """Monotonic timestamp for the next insert in *session_id*."""
        last_at = await self._max_message_created_at(db, session_id)
        now = utc_now()
        if last_at is None:
            return now
        bumped = last_at + timedelta(microseconds=1)
        return max(now, bumped)

    async def _message_cache_generation(self, session_id: UUID) -> int:
        if self._cache is None:
            return 0
        return await self._cache.get_generation(
            f"messages:{session_id}", namespace="chat"
        )

    async def _invalidate_session_message_cache(self, session_id: UUID) -> None:
        """Drop cached history/list keys for a session after writes.

        History pages are versioned by a monotonic generation counter, so a
        single O(1) ``bump_generation`` retires every cached page for the
        session without an O(n) prefix scan; stale pages are simply never hit
        again because readers fold the new generation into their key.
        """
        if self._cache is None:
            return
        await self._cache.bump_generation(
            f"messages:{session_id}", namespace="chat"
        )
        await self._cache.delete(f"session:{session_id}", namespace="chat")

    async def create_session(
        self,
        user_id: UUID,
        *,
        name: str | None = None,
        flow_id: UUID | None = None,
        project_id: UUID | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ChatSession:
        """Create a new chat session.

        Args:
            user_id: Owner of the session
            name: Optional session name
            flow_id: Optional associated flow
            project_id: Optional chat project grouping
            metadata: Optional session metadata

        Returns:
            The created session
        """
        if self._db is None:
            raise RuntimeError("Database service not initialized")

        session = ChatSession(
            id=uuid4(),
            user_id=user_id,
            name=name,
            flow_id=flow_id,
            project_id=project_id,
            session_metadata=json.dumps(metadata) if metadata else None,
        )

        async with self._db.session() as db:
            if session_dialect_name(db) == "sqlite":
                await db.execute(
                    text(
                        """
                        INSERT INTO chat_sessions (
                            created_at, updated_at, id, name, user_id,
                            flow_id, project_id, workspace_id, is_active,
                            message_count, last_message_at, session_metadata
                        )
                        VALUES (
                            :created_at, :updated_at, :id, :name, :user_id,
                            :flow_id, :project_id, :workspace_id, :is_active,
                            :message_count, :last_message_at, :session_metadata
                        )
                        """
                    ),
                    {
                        "created_at": session.created_at,
                        "updated_at": session.updated_at,
                        "id": session.id.hex,
                        "name": session.name,
                        "user_id": await sqlite_parent_id_text(db, "users", user_id),
                        "flow_id": (
                            await sqlite_parent_id_text(db, "flows", flow_id)
                            if flow_id is not None
                            else None
                        ),
                        "project_id": (
                            await sqlite_parent_id_text(db, "chat_projects", project_id)
                            if project_id is not None
                            else None
                        ),
                        "workspace_id": (
                            await sqlite_parent_id_text(
                                db, "workspaces", session.workspace_id
                            )
                            if session.workspace_id is not None
                            else None
                        ),
                        "is_active": bool(session.is_active),
                        "message_count": session.message_count,
                        "last_message_at": session.last_message_at,
                        "session_metadata": session.session_metadata,
                    },
                )
                loaded = await load_chat_session_by_id(
                    db, session.id, owner_user_id=None
                )
                if loaded is None:
                    raise RuntimeError("Inserted chat session could not be reloaded")
                session = loaded
            else:
                db.add(session)
                await db.flush()
                await db.refresh(session)

        if self._cache:
            await self._cache.set(
                f"session:{session.id}",
                session.model_dump(mode="json"),
                namespace="chat",
                ttl=CACHE_SESSION_TTL,
            )

        logger.debug("Created chat session %s for user %s", session.id, user_id)
        return session

    async def get_session(
        self,
        session_id: UUID,
        *,
        user_id: UUID | None = None,
    ) -> ChatSession | None:
        """Get a chat session by ID.

        Args:
            session_id: The session ID
            user_id: Optional user ID for ownership verification

        Returns:
            The session or None if not found
        """
        if self._cache:
            cached: Any = await self._cache.get(f"session:{session_id}", namespace="chat")
            if cached:
                try:
                    cached_session = ChatSession.model_validate(cached)
                except ValidationError:
                    logger.warning(
                        "Invalid cached chat session %s; evicting cache entry",
                        session_id,
                    )
                    await self._cache.delete(f"session:{session_id}", namespace="chat")
                else:
                    if user_id is None or same_user_id(cached_session.user_id, user_id):
                        return cached_session

        if self._db is None:
            return None

        async with self._db.session() as db:
            session = await load_chat_session_by_id(
                db, session_id, owner_user_id=user_id if user_id else None
            )

        if session and self._cache:
            await self._cache.set(
                f"session:{session_id}",
                session.model_dump(mode="json"),
                namespace="chat",
                ttl=CACHE_SESSION_TTL,
            )

        return session

    async def fork_session(
        self,
        user_id: UUID,
        session_id: UUID,
        *,
        up_to_message_id: UUID,
        new_name: str | None = None,
    ) -> ChatSession:
        """Fork a session by copying the message prefix up to *up_to_message_id*.

        The new session keeps the source's flow/project bindings and gets a
        fresh copy of every user/assistant message (system rows are skipped;
        tool-call JSON and extensions are preserved verbatim). Message IDs are
        new — the fork is a content copy, not a reference.

        Raises:
            ValueError: If *up_to_message_id* is not part of the session.
        """
        if self._db is None:
            raise RuntimeError("Database service not initialized")

        source = await self.get_session(session_id, user_id=user_id)
        if source is None:
            raise ValueError(f"Session {session_id} not found")

        messages = await self._db.repositories.messages.list_for_session(
            session_id, limit=10000
        )
        prefix: list[Message] = []
        for message in messages:
            prefix.append(message)
            if message.id == up_to_message_id:
                break
        else:
            raise ValueError(f"Message {up_to_message_id} not found in session {session_id}")

        forked = await self.create_session(
            user_id,
            name=new_name or f"Fork: {source.name or 'Chat'}",
            flow_id=source.flow_id,
            project_id=source.project_id,
        )

        # Copy the prefix in ONE transaction (add_message opens a session,
        # session reload, MAX(created_at) subquery and a chat_sessions UPDATE
        # per row — ~10 queries per copied message). Mirrors
        # replace_session_transcript's batch insert shape.
        copy_rows = [
            m for m in prefix if m.role in (MessageRole.USER, MessageRole.ASSISTANT)
        ]
        if copy_rows:
            async with self._db.session() as db:
                chat_row = await load_chat_session_by_id(
                    db, forked.id, owner_user_id=None
                )
                if chat_row is None:
                    raise RuntimeError("Forked chat session could not be reloaded")
                sqlite = session_dialect_name(db) == "sqlite"
                uid = chat_row.user_id
                wid = chat_row.workspace_id
                fid = chat_row.flow_id
                now_db = utc_now()
                prev_at: datetime | None = None

                for message in copy_rows:
                    row_created = naive_utc_for_db_column(message.created_at) or now_db
                    if prev_at is not None and row_created <= prev_at:
                        row_created = prev_at + timedelta(microseconds=1)
                    prev_at = row_created
                    row = Message(
                        id=uuid4(),
                        session_id=forked.id,
                        user_id=uid if message.role == MessageRole.USER else None,
                        workspace_id=wid,
                        flow_id=fid,
                        role=message.role,
                        content=message.content,
                        model=message.model,
                        tool_calls=message.tool_calls,
                        attachments=message.attachments,
                        input_tokens=message.input_tokens,
                        output_tokens=message.output_tokens,
                        latency_ms=message.latency_ms,
                        extensions=message.extensions,
                        status=MessageStatus.COMPLETED,
                        created_at=row_created,
                    )
                    if sqlite:
                        await sqlite_insert_message(db, row)
                    else:
                        db.add(row)

                if sqlite:
                    await _sqlite_update_chat_session(
                        db,
                        forked.id,
                        {
                            "message_count": len(copy_rows),
                            "last_message_at": now_db,
                            "updated_at": now_db,
                        },
                    )
                else:
                    chat_row.message_count = len(copy_rows)
                    chat_row.last_message_at = now_db
                    chat_row.updated_at = now_db

            await self._invalidate_session_message_cache(forked.id)

        return forked

    async def list_sessions(
        self,
        user_id: UUID,
        *,
        active_only: bool = True,
        flow_id: UUID | None = None,
        project_id: UUID | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> list[SessionRead]:
        """List chat sessions for a user.

        Args:
            user_id: The user ID
            active_only: Only return active sessions
            project_id: Optional project filter
            offset: Pagination offset
            limit: Maximum number of sessions

        Returns:
            List of sessions
        """
        if self._db is None:
            return []

        async with self._db.session() as db:
            # One index-backed ORM query for both dialects: uuid_column_equals
            # is a pass-through on PostgreSQL and an expression-indexed
            # predicate on SQLite (the old path fetched ids then re-loaded
            # each session — one query per row).
            stmt = (
                select(ChatSession)
                .where(uuid_column_equals(db, ChatSession.user_id, user_id))
                .order_by(col(ChatSession.updated_at).desc())
                .offset(offset)
                .limit(limit)
            )
            if active_only:
                stmt = stmt.where(col(ChatSession.is_active).is_(True))
            if flow_id is not None:
                stmt = stmt.where(uuid_column_equals(db, ChatSession.flow_id, flow_id))
            if project_id is not None:
                stmt = stmt.where(uuid_column_equals(db, ChatSession.project_id, project_id))

            result = await db.execute(stmt)
            sessions = list(result.scalars().all())

        return [chat_session_to_read(s) for s in sessions]

    async def count_sessions(
        self,
        user_id: UUID,
        *,
        active_only: bool = True,
        flow_id: UUID | None = None,
        project_id: UUID | None = None,
    ) -> int:
        """Return the total number of sessions matching the filter criteria."""
        if self._db is None:
            return 0
        from sqlmodel import func as sqlfunc

        async with self._db.session() as db:
            stmt = select(sqlfunc.count()).select_from(ChatSession).where(
                uuid_column_equals(db, ChatSession.user_id, user_id)
            )
            if active_only:
                stmt = stmt.where(col(ChatSession.is_active).is_(True))
            if flow_id is not None:
                stmt = stmt.where(uuid_column_equals(db, ChatSession.flow_id, flow_id))
            if project_id is not None:
                stmt = stmt.where(uuid_column_equals(db, ChatSession.project_id, project_id))
            result = await db.execute(stmt)
            return int(result.scalar_one())

    async def move_session_to_project(
        self,
        session_id: UUID,
        *,
        user_id: UUID,
        project_id: UUID | None,
    ) -> ChatSession | None:
        """Move a session into a chat project, or detach it when ``project_id`` is ``None``."""
        if self._db is None:
            return None
        async with self._db.session() as db:
            session = await load_chat_session_by_id(db, session_id, owner_user_id=user_id)
            if session is None:
                return None
            updated_at = utc_now()
            if session_dialect_name(db) == "sqlite":
                project_id_text = (
                    await sqlite_parent_id_text(db, "chat_projects", project_id)
                    if project_id is not None
                    else None
                )
                await db.execute(
                    text(
                        """
                        UPDATE chat_sessions
                        SET project_id = :project_id, updated_at = :updated_at
                        WHERE lower(replace(CAST(id AS TEXT), '-', '')) = :session_id
                        """
                    ),
                    {
                        "project_id": project_id_text,
                        "updated_at": updated_at,
                        "session_id": str(session_id).replace("-", "").lower(),
                    },
                )
                db.expunge(session)
                session.project_id = project_id
                session.updated_at = updated_at
            else:
                session.project_id = project_id
                session.updated_at = updated_at
                db.add(session)
                await db.flush()
                await db.refresh(session)
        if self._cache:
            await self._cache.delete(f"session:{session_id}", namespace="chat")
        return session

    async def filter_message_ids_for_session(
        self,
        session_id: UUID,
        candidate_ids: list[UUID],
    ) -> list[UUID]:
        """Return *candidate_ids* in original order, keeping only rows in *session_id*."""
        if not candidate_ids or self._db is None:
            return []
        async with self._db.session() as db:
            sid_eq = await _message_session_sid_eq(db, session_id)
            result = await db.execute(
                select(Message.id).where(
                    sid_eq,
                    uuid_column_in(db, Message.id, candidate_ids),
                )
            )
            found = {parse_uuid_stored(str(row[0])) for row in result.all()}
        return [mid for mid in candidate_ids if mid in found]

    async def sanitize_metadata_patch(
        self,
        session_id: UUID,
        patch: dict[str, Any],
    ) -> dict[str, Any]:
        """Copy of *patch* with ``pinned_message_ids`` restricted to this session.

        Invalid UUIDs are dropped. Ids that are not messages in *session_id*
        are removed. ``pinned_message_ids: null`` passes through (clears key in
        merge). Non-list values are treated as no candidates.
        """
        if not patch:
            return {}
        out = dict(patch)
        if "pinned_message_ids" not in out:
            return out
        val = out["pinned_message_ids"]
        if val is None:
            return out
        candidates: list[UUID] = []
        for x in val if isinstance(val, list) else []:
            try:
                candidates.append(UUID(str(x)))
            except (ValueError, TypeError):
                continue
        filtered = await self.filter_message_ids_for_session(session_id, candidates)
        out["pinned_message_ids"] = [str(u) for u in filtered]
        return out

    async def merge_session_metadata(
        self,
        session_id: UUID,
        user_id: UUID,
        *,
        patch: dict[str, Any],
    ) -> ChatSession | None:
        """Shallow-merge ``patch`` into ``session.session_metadata`` (JSON).

        ``None`` values inside ``patch`` clear the corresponding key.
        Returns the updated session, or ``None`` if not found /
        not owned by ``user_id``.
        """
        if self._db is None or not patch:
            return None
        async with self._db.session() as db:
            session = await load_chat_session_by_id(db, session_id, owner_user_id=user_id)
            if not session:
                return None
            current: dict[str, Any] = {}
            if session.session_metadata:
                try:
                    parsed = json.loads(session.session_metadata)
                    if isinstance(parsed, dict):
                        current = parsed
                except (TypeError, ValueError):
                    current = {}
            for key, value in patch.items():
                if value is None:
                    current.pop(key, None)
                else:
                    current[key] = value
            session_metadata = json.dumps(current) if current else None
            updated_at = utc_now()
            if session_dialect_name(db) == "sqlite":
                await _sqlite_update_chat_session(
                    db,
                    session_id,
                    {
                        "session_metadata": session_metadata,
                        "updated_at": updated_at,
                    },
                )
                db.expunge(session)
                session.session_metadata = session_metadata
                session.updated_at = updated_at
            else:
                session.session_metadata = session_metadata
                session.updated_at = updated_at
                await db.flush()
                await db.refresh(session)

        if self._cache:
            await self._cache.set(
                f"session:{session_id}",
                session.model_dump(mode="json"),
                namespace="chat",
                ttl=CACHE_SESSION_TTL,
            )
        return session

    async def list_authorized_roots(
        self,
        session_id: UUID,
        *,
        user_id: UUID,
    ) -> list[dict[str, Any]]:
        """Directories the user granted for this chat session (from ``session_metadata``)."""
        session = await self.get_session(session_id, user_id=user_id)
        if session is None:
            return []
        return parse_authorized_roots_from_session_metadata(session.session_metadata)

    async def add_authorized_root(
        self,
        session_id: UUID,
        user_id: UUID,
        *,
        path: str,
        label: str | None = None,
    ) -> list[dict[str, Any]]:
        """Append a resolved absolute directory to ``authorized_roots`` metadata."""
        from StAgent.project.paths import (
            ProjectPathSafetyError,
            validate_project_path,
        )

        raw = (path or "").strip()
        if not raw:
            raise ValueError("path must be non-empty")
        try:
            resolved = validate_project_path(raw)
        except ProjectPathSafetyError as exc:
            raise ValueError(str(exc)) from exc
        path_str = str(resolved)
        existing = await self.list_authorized_roots(session_id, user_id=user_id)
        for e in existing:
            if e.get("path") == path_str:
                return existing
        entry: dict[str, Any] = {"path": path_str}
        if label and str(label).strip():
            entry["label"] = str(label).strip()
        new_list = existing + [entry]
        await self.merge_session_metadata(
            session_id, user_id, patch={AUTHORIZED_ROOTS_META_KEY: new_list},
        )
        return new_list

    async def remove_authorized_root(
        self,
        session_id: UUID,
        user_id: UUID,
        *,
        path: str,
    ) -> list[dict[str, Any]]:
        """Remove one granted directory (matched on resolved ``path`` string)."""
        session = await self.get_session(session_id, user_id=user_id)
        if session is None:
            return []
        existing = parse_authorized_roots_from_session_metadata(session.session_metadata)
        try:
            target = str(Path(path).expanduser().resolve(strict=False))
        except Exception:  # noqa: BLE001
            target = (path or "").strip()
        new_list = [e for e in existing if e.get("path") != target]
        if len(new_list) == len(existing):
            stripped = (path or "").strip()
            new_list = [e for e in existing if e.get("path") != stripped]
        await self.merge_session_metadata(
            session_id,
            user_id,
            patch={
                AUTHORIZED_ROOTS_META_KEY: (new_list if new_list else None),
            },
        )
        return new_list

    async def update_session(
        self,
        session_id: UUID,
        user_id: UUID,
        *,
        name: str | None = None,
        is_active: bool | None = None,
    ) -> ChatSession | None:
        """Update a chat session's mutable fields.

        Returns the updated session, or ``None`` if not found / not
        owned by *user_id*.
        """
        if self._db is None:
            return None

        async with self._db.session() as db:
            session = await load_chat_session_by_id(db, session_id, owner_user_id=user_id)

            if not session:
                return None

            updated_at = utc_now()
            if session_dialect_name(db) == "sqlite":
                values: dict[str, Any] = {"updated_at": updated_at}
                if name is not None:
                    values["name"] = name
                if is_active is not None:
                    values["is_active"] = is_active
                await _sqlite_update_chat_session(db, session_id, values)
                db.expunge(session)
                if name is not None:
                    session.name = name
                if is_active is not None:
                    session.is_active = is_active
                session.updated_at = updated_at
            else:
                if name is not None:
                    session.name = name
                if is_active is not None:
                    session.is_active = is_active
                session.updated_at = updated_at
                await db.flush()
                await db.refresh(session)

        if self._cache:
            await self._cache.set(
                f"session:{session_id}",
                session.model_dump(mode="json"),
                namespace="chat",
                ttl=CACHE_SESSION_TTL,
            )

        return session

    async def get_messages_paginated(
        self,
        session_id: UUID,
        *,
        page: int = 1,
        page_size: int = 50,
        role: MessageRole | None = None,
        before: datetime | None = None,
        after: datetime | None = None,
        order: str = "asc",
    ) -> tuple[list[MessageRead], int]:
        """Return a page of messages with total count.

        Returns ``(items, total)`` so the caller can build a
        :class:`PaginatedResponse`.
        """
        if self._db is None:
            return [], 0

        async with self._db.session() as db:
            sid_eq = await _message_session_sid_eq(db, session_id)
            base = select(Message).where(sid_eq)
            if role is not None:
                base = base.where(Message.role == role)
            if before is not None:
                base = base.where(Message.created_at < before)
            if after is not None:
                base = base.where(Message.created_at > after)

            from sqlmodel import func as sqlfunc

            count_stmt = select(sqlfunc.count()).select_from(base.subquery())
            total = (await db.execute(count_stmt)).scalar_one()

            if order == "desc":
                order_clauses = (col(Message.created_at).desc(), col(Message.id).asc())
            else:
                order_clauses = (col(Message.created_at).asc(), col(Message.id).asc())
            page_stmt = base.order_by(*order_clauses).offset((page - 1) * page_size).limit(page_size)
            rows = (await db.execute(page_stmt)).scalars().all()

        return [MessageRead.model_validate(m) for m in rows], total

    async def delete_session(
        self,
        session_id: UUID,
        user_id: UUID,
        *,
        soft: bool = True,
    ) -> bool:
        """Delete a chat session.

        Args:
            session_id: The session to delete
            user_id: The owner for verification
            soft: If True, marks inactive instead of deleting

        Returns:
            True if session was deleted/deactivated
        """
        if self._db is None:
            return False

        async with self._db.session() as db:
            chat = await load_chat_session_by_id(db, session_id, owner_user_id=user_id)

            if not chat:
                return False

            if soft:
                if session_dialect_name(db) == "sqlite":
                    await _sqlite_update_chat_session(
                        db,
                        session_id,
                        {"is_active": False, "updated_at": utc_now()},
                    )
                else:
                    chat.is_active = False
                    chat.updated_at = utc_now()
            else:
                await _delete_hard_session_dependents(db, session_id)
                if session_dialect_name(db) == "sqlite":
                    await db.execute(
                        text(
                            """
                            DELETE FROM chat_sessions
                            WHERE lower(replace(CAST(id AS TEXT), '-', '')) = :session_id
                            """
                        ),
                        {"session_id": str(session_id).replace("-", "").lower()},
                    )
                else:
                    await db.delete(chat)

        if self._cache:
            await self._cache.delete(f"session:{session_id}", namespace="chat")

        return True

    async def cleanup_expired_sessions(
        self,
        *,
        older_than_days: int | None = None,
        hard: bool = True,
        batch_size: int = 100,
    ) -> dict[str, int]:
        """Remove inactive chat sessions and their conversation rows after TTL."""
        if self._db is None:
            return {"sessions": 0, "messages": 0, "canvas_documents": 0}

        ttl_days = (
            older_than_days
            if older_than_days is not None
            else getattr(self._settings.session, "inactive_session_ttl_days", 90)
        )
        cutoff = datetime.utcnow() - timedelta(days=max(1, int(ttl_days)))
        deleted_sessions = 0
        deleted_messages = 0
        deleted_canvas = 0
        expired_ids: list[UUID] = []

        async with self._db.session() as db:
            stmt = (
                select(ChatSession)
                .where(ChatSession.is_active == False)  # noqa: E712
                .where(col(ChatSession.updated_at) < cutoff)
                .limit(max(1, batch_size))
            )
            expired = list((await db.execute(stmt)).scalars().all())
            expired_ids = [s.id for s in expired]

            for chat in expired:
                if hard:
                    c_canv, _ep, _fi, c_msg = await _delete_hard_session_dependents(db, chat.id)
                    deleted_canvas += c_canv
                    deleted_messages += c_msg
                    if session_dialect_name(db) == "sqlite":
                        await db.execute(
                            text(
                                """
                                DELETE FROM chat_sessions
                                WHERE lower(replace(CAST(id AS TEXT), '-', '')) = :session_id
                                """
                            ),
                            {
                                "session_id": str(chat.id).replace("-", "").lower(),
                            },
                        )
                    else:
                        await db.delete(chat)
                else:
                    if session_dialect_name(db) == "sqlite":
                        await _sqlite_update_chat_session(
                            db,
                            chat.id,
                            {"updated_at": utc_now()},
                        )
                    else:
                        chat.updated_at = utc_now()
                deleted_sessions += 1

        if self._cache:
            for session_id in expired_ids:
                await self._invalidate_session_message_cache(session_id)

        return {
            "sessions": deleted_sessions,
            "messages": deleted_messages,
            "canvas_documents": deleted_canvas,
        }

    async def add_message(
        self,
        session_id: UUID,
        role: MessageRole,
        content: str,
        *,
        user_id: UUID | None = None,
        flow_id: UUID | None = None,
        model: str | None = None,
        tool_calls: list[dict[str, Any]] | None = None,
        tool_call_id: str | None = None,
        attachments: list[str] | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        latency_ms: int | None = None,
        status: MessageStatus = MessageStatus.COMPLETED,
        parent_id: UUID | None = None,
        extensions: str | None = None,
    ) -> Message:
        """Add a message to a chat session.

        Args:
            session_id: The session ID
            role: Message role (user/assistant/system/tool)
            content: Message content
            user_id: Optional sender user ID
            flow_id: Optional associated flow
            model: Optional model name (for assistant messages)
            tool_calls: Optional tool call data
            tool_call_id: Optional tool call ID (for tool messages)
            attachments: Optional file attachments
            extensions: Optional JSON string for structured UI (e.g. chat_workflow)
            input_tokens: Optional input token count
            output_tokens: Optional output token count
            latency_ms: Optional response latency
            status: Message status
            parent_id: Optional parent message ID

        Returns:
            The created message
        """
        if self._db is None:
            raise RuntimeError("Database service not initialized")

        if input_tokens is None and role == MessageRole.USER:
            input_tokens = estimate_tokens(content)
        if output_tokens is None and role == MessageRole.ASSISTANT:
            output_tokens = estimate_tokens(content)

        total_tokens = None
        if input_tokens is not None or output_tokens is not None:
            total_tokens = (input_tokens or 0) + (output_tokens or 0)

        message = Message(
            id=uuid4(),
            session_id=session_id,
            user_id=user_id,
            flow_id=flow_id,
            role=role,
            content=content,
            model=model,
            tool_calls=json.dumps(tool_calls) if tool_calls else None,
            tool_call_id=tool_call_id,
            attachments=json.dumps(attachments) if attachments else None,
            extensions=extensions,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            latency_ms=latency_ms,
            status=status,
            parent_id=parent_id,
        )

        async with self._db.session() as db:
            # Avoid autoflush ordering issues: load session row before flushing new message.
            sqlite = session_dialect_name(db) == "sqlite"
            with db.no_autoflush:
                chat_row = await load_chat_session_by_id(
                    db, session_id, owner_user_id=None
                )
                message.created_at = await self._next_message_created_at(db, session_id)
                if chat_row:
                    now = utc_now()
                    if sqlite:
                        await db.execute(
                            text(
                                """
                                UPDATE chat_sessions
                                SET message_count = COALESCE(message_count, 0) + 1,
                                    last_message_at = :now,
                                    updated_at = :now
                                WHERE lower(replace(CAST(id AS TEXT), '-', '')) = :session_id
                                """
                            ),
                            {
                                "now": now,
                                "session_id": str(session_id).replace("-", "").lower(),
                            },
                        )
                    else:
                        chat_row.message_count += 1
                        chat_row.last_message_at = now
                        chat_row.updated_at = now
                if sqlite:
                    message = await sqlite_insert_message(db, message)
                else:
                    db.add(message)

            await db.flush()
            if not sqlite:
                await db.refresh(message)

        await self._invalidate_session_message_cache(session_id)

        return message

    async def replace_session_transcript(
        self,
        session_id: UUID,
        user_id: UUID,
        messages: list[Any],
    ) -> bool:
        """Replace all persisted ``messages`` rows with a new transcript (e.g. after compaction).

        Expects :class:`~StAgent.services.session.state.SessionMessage` instances.
        """
        if self._db is None:
            raise RuntimeError("Database service not initialized")

        chat = await self.get_session(session_id, user_id=user_id)
        if chat is None:
            return False

        from StAgent.services.session.state import SessionMessage

        async with self._db.session() as db:
            chat_row = await load_chat_session_by_id(db, session_id, owner_user_id=user_id)
            if chat_row is None:
                return False

            sqlite = session_dialect_name(db) == "sqlite"
            sid_eq = await _message_session_sid_eq(db, session_id)

            await db.execute(delete(Message).where(sid_eq))

            uid = chat_row.user_id
            wid = chat_row.workspace_id
            fid = chat_row.flow_id
            now_db = utc_now()
            prev_at: datetime | None = None

            for sm in messages:
                if not isinstance(sm, SessionMessage):
                    raise TypeError("messages must be SessionMessage instances")
                role_raw = str(sm.role or "user").lower()
                try:
                    role_enum = MessageRole(role_raw)
                except ValueError:
                    role_enum = MessageRole.USER
                row_created = naive_utc_for_db_column(sm.created_at) or now_db
                if prev_at is not None and row_created <= prev_at:
                    row_created = prev_at + timedelta(microseconds=1)
                prev_at = row_created
                row = Message(
                    id=sm.id,
                    session_id=session_id,
                    user_id=uid,
                    workspace_id=wid,
                    flow_id=fid,
                    role=role_enum,
                    content=sm.content or "",
                    model=sm.model,
                    tool_calls=json.dumps(sm.tool_calls) if sm.tool_calls else None,
                    tool_call_id=sm.tool_call_id,
                    attachments=(
                        json.dumps(sm.attachment_ids) if sm.attachment_ids else None
                    ),
                    status=MessageStatus.COMPLETED,
                    created_at=row_created,
                )
                if sqlite:
                    await sqlite_insert_message(db, row)
                else:
                    db.add(row)

            if sqlite:
                await _sqlite_update_chat_session(
                    db,
                    session_id,
                    {
                        "message_count": len(messages),
                        "last_message_at": now_db,
                        "updated_at": now_db,
                    },
                )
            else:
                chat_row.message_count = len(messages)
                chat_row.last_message_at = now_db
                chat_row.updated_at = now_db

        await self._invalidate_session_message_cache(session_id)

        return True

    async def replace_tool_message_if_pending(
        self,
        session_id: UUID,
        tool_call_id: str,
        new_content: str,
        *,
        user_id: UUID | None = None,
    ) -> bool:
        """Replace a placeholder ``ask_user`` tool row (``_wa_pending``) if present."""
        if self._db is None:
            return False
        token = "_wa_pending"
        async with self._db.session() as db:
            sid_eq = await _message_session_sid_eq(db, session_id)
            stmt = (
                select(Message)
                .where(sid_eq)
                .where(Message.role == MessageRole.TOOL)
                .where(Message.tool_call_id == tool_call_id)
                .order_by(col(Message.created_at).desc())
            )
            result = await db.execute(stmt)
            row = result.scalars().first()
            if row is None or token not in (row.content or ""):
                return False
            if session_dialect_name(db) == "sqlite":
                values: dict[str, Any] = {
                    "content": new_content,
                    "updated_at": utc_now(),
                }
                if user_id is None:
                    await _sqlite_update_message(db, row.id, values)
                else:
                    await db.execute(
                        text(
                            """
                            UPDATE messages
                            SET content = :content,
                                user_id = :user_id,
                                updated_at = :updated_at
                            WHERE lower(replace(CAST(id AS TEXT), '-', '')) = :message_id
                            """
                        ),
                        {
                            **values,
                            "user_id": await sqlite_parent_id_text(db, "users", user_id),
                            "message_id": str(row.id).replace("-", "").lower(),
                        },
                    )
            else:
                row.content = new_content
                row.updated_at = utc_now()
                if user_id is not None:
                    row.user_id = user_id
                await db.commit()

        await self._invalidate_session_message_cache(session_id)

        return True

    async def get_history(
        self,
        session_id: UUID,
        *,
        user_id: UUID | None = None,
        limit: int = 100,
        before_id: UUID | None = None,
        include_system: bool = True,
    ) -> list[MessageRead]:
        """Get message history for a session.

        Args:
            session_id: The session ID
            user_id: Optional owner scope for the cache key (never derived from
                untrusted query text)
            limit: Maximum messages to return
            before_id: Only return messages before this ID
            include_system: Include system messages

        Returns:
            List of messages in chronological order
        """
        if self._db is None:
            return []

        session = await self.get_session(session_id, user_id=user_id)
        if session is None:
            return []

        cache_key: str | None = None
        if self._cache:
            generation = await self._message_cache_generation(session_id)
            cache_key = build_scoped_cache_key(
                "messages",
                owner_id=session.user_id,
                resource_id=session_id,
                generation=generation,
                vary={
                    "limit": limit,
                    "before_id": before_id,
                    "include_system": include_system,
                },
            )
            cached: Any = await self._cache.get(cache_key, namespace="chat")
            if cached:
                return [MessageRead.model_validate(m) for m in cached]

        async with self._db.session() as db:
            sid_eq = await _message_session_sid_eq(db, session_id)
            # Fetch the NEWEST window via DESC + LIMIT (index-assisted by the
            # composite expression index, scanned backwards), then restore
            # chronological order. The previous ASC + LIMIT returned the
            # OLDEST rows, so long sessions fed the LLM their opening
            # messages instead of recent context.
            stmt = (
                select(Message)
                .where(sid_eq)
                .order_by(col(Message.created_at).desc(), col(Message.id).desc())
                .limit(limit)
            )

            if before_id:
                subq = select(Message.created_at).where(
                    uuid_column_equals(db, Message.id, before_id)
                )
                stmt = stmt.where(Message.created_at < subq.scalar_subquery())

            if not include_system:
                stmt = stmt.where(Message.role != MessageRole.SYSTEM)

            result = await db.execute(stmt)
            messages = list(result.scalars().all())

        messages.reverse()
        message_reads = [MessageRead.model_validate(m) for m in messages]

        if self._cache and cache_key is not None:
            await self._cache.set(
                cache_key,
                [m.model_dump(mode="json") for m in message_reads],
                namespace="chat",
                ttl=CACHE_MESSAGES_TTL,
            )

        return message_reads

    async def get_session_message(
        self,
        session_id: UUID,
        message_id: UUID,
        *,
        user_id: UUID,
    ) -> Message | None:
        """Return a message row if it belongs to the session and user owns the session."""
        if self._db is None:
            return None
        session = await self.get_session(session_id, user_id=user_id)
        if session is None:
            return None
        async with self._db.session() as db:
            sid_eq = await _message_session_sid_eq(db, session_id)
            stmt = select(Message).where(
                uuid_column_equals(db, Message.id, message_id),
                sid_eq,
            )
            result = await db.execute(stmt)
            return result.scalar_one_or_none()

    async def get_context_messages(
        self,
        session_id: UUID,
        *,
        user_id: UUID | None = None,
        max_tokens: int | None = None,
        max_messages: int = 50,
    ) -> list[dict[str, Any]]:
        """Get messages formatted for LLM context.

        Args:
            session_id: The session ID
            user_id: Optional owner scope forwarded to :meth:`get_history`
            max_tokens: Maximum total tokens (approximate)
            max_messages: Maximum number of messages

        Returns:
            List of message dicts in OpenAI format
        """
        messages = await self.get_history(
            session_id, user_id=user_id, limit=max_messages
        )

        if max_tokens is None:
            max_tokens = self._settings.agent.context_window_tokens

        result: list[dict[str, Any]] = []
        total_tokens = 0

        for msg in reversed(messages):
            tokens = estimate_tokens(msg.content)
            if total_tokens + tokens > max_tokens:
                break

            msg_dict: dict[str, Any] = {
                "role": msg.role.value,
                "content": msg.content,
            }

            if msg.tool_calls:
                msg_dict["tool_calls"] = msg.tool_calls

            if msg.tool_call_id:
                msg_dict["tool_call_id"] = msg.tool_call_id

            if msg.role == MessageRole.ASSISTANT and msg.extensions:
                rc = msg.extensions.get("reasoning_content")
                if isinstance(rc, str) and rc.strip():
                    msg_dict["reasoning_content"] = rc

            result.insert(0, msg_dict)
            total_tokens += tokens

        return result

    async def count_tokens(
        self,
        session_id: UUID,
        *,
        since: datetime | None = None,
    ) -> dict[str, int]:
        """Count tokens used in a session.

        Args:
            session_id: The session ID
            since: Only count tokens after this time

        Returns:
            Token counts by category
        """
        if self._db is None:
            return {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}

        async with self._db.session() as db:
            sid_eq = await _message_session_sid_eq(db, session_id)
            stmt = select(Message).where(sid_eq)
            if since:
                stmt = stmt.where(Message.created_at >= since)

            result = await db.execute(stmt)
            messages = result.scalars().all()

        input_tokens = sum(m.input_tokens or 0 for m in messages)
        output_tokens = sum(m.output_tokens or 0 for m in messages)

        return {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
            "message_count": len(messages),
        }

    async def stream_messages(
        self,
        session_id: UUID,
        *,
        after_id: UUID | None = None,
    ) -> AsyncIterator[Message]:
        """Stream new messages for a session (for real-time updates).

        Args:
            session_id: The session ID
            after_id: Only yield messages after this ID

        Yields:
            New messages as they arrive
        """
        if self._db is None:
            return

        last_check = utc_now()
        if after_id:
            async with self._db.session() as db:
                stmt = select(Message.created_at).where(
                    uuid_column_equals(db, Message.id, after_id)
                )
                result = await db.execute(stmt)
                row = result.first()
                if row:
                    last_check = row[0]

        while True:
            async with self._db.session() as db:
                sid_eq = await _message_session_sid_eq(db, session_id)
                message_stmt = (
                    select(Message)
                    .where(
                        sid_eq,
                        Message.created_at > last_check,
                    )
                    .order_by(col(Message.created_at).asc(), col(Message.id).asc())
                )
                result = await db.execute(message_stmt)
                messages = list(result.scalars().all())

            for msg in messages:
                yield msg
                last_check = msg.created_at

            import asyncio

            await asyncio.sleep(0.5)

    async def update_message_status(
        self,
        message_id: UUID,
        status: MessageStatus,
        *,
        error: str | None = None,
    ) -> bool:
        """Update the status of a message.

        Args:
            message_id: The message ID
            status: New status
            error: Optional error message

        Returns:
            True if updated successfully
        """
        if self._db is None:
            return False

        async with self._db.session() as db:
            stmt = select(Message).where(uuid_column_equals(db, Message.id, message_id))
            result = await db.execute(stmt)
            message = result.scalar_one_or_none()

            if not message:
                return False

            updated_at = utc_now()
            if session_dialect_name(db) == "sqlite":
                values: dict[str, Any] = {"status": status, "updated_at": updated_at}
                if error:
                    values["error"] = error
                await _sqlite_update_message(db, message_id, values)
            else:
                message.status = status
                message.updated_at = updated_at
                if error:
                    message.error = error

        return True

    async def add_feedback(
        self,
        message_id: UUID,
        *,
        rating: int | None = None,
        feedback: str | None = None,
    ) -> bool:
        """Add feedback to a message.

        Args:
            message_id: The message ID
            rating: Optional 1-5 rating
            feedback: Optional text feedback

        Returns:
            True if updated successfully
        """
        if self._db is None:
            return False

        async with self._db.session() as db:
            stmt = select(Message).where(uuid_column_equals(db, Message.id, message_id))
            result = await db.execute(stmt)
            message = result.scalar_one_or_none()

            if not message:
                return False

            updated_at = utc_now()
            values: dict[str, Any] = {"updated_at": updated_at}
            if rating is not None:
                values["rating"] = max(1, min(5, rating))
            if feedback is not None:
                values["feedback"] = feedback[:2000]
            if session_dialect_name(db) == "sqlite":
                await _sqlite_update_message(db, message_id, values)
            else:
                if "rating" in values:
                    message.rating = values["rating"]
                if "feedback" in values:
                    message.feedback = values["feedback"]
                message.updated_at = updated_at

        return True

    async def patch_assistant_message_rating(
        self,
        session_id: UUID,
        message_id: UUID,
        *,
        user_id: UUID,
        rating: int | None,
    ) -> bool:
        """Set thumbs feedback on an assistant message (1 or 5) or clear with ``rating=None``."""
        if self._db is None:
            return False
        session = await self.get_session(session_id, user_id=user_id)
        if session is None:
            return False

        async with self._db.session() as db:
            sid_eq = await _message_session_sid_eq(db, session_id)
            stmt = select(Message).where(
                uuid_column_equals(db, Message.id, message_id),
                sid_eq,
                Message.role == MessageRole.ASSISTANT,
            )
            result = await db.execute(stmt)
            message = result.scalar_one_or_none()
            if not message:
                return False

            updated_at = utc_now()
            value = None if rating is None else max(1, min(5, int(rating)))
            if session_dialect_name(db) == "sqlite":
                await _sqlite_update_message(
                    db,
                    message_id,
                    {"rating": value, "updated_at": updated_at},
                )
            else:
                message.rating = value
                message.updated_at = updated_at

        await self._invalidate_session_message_cache(session_id)

        return True

    async def merge_message_extensions(
        self,
        session_id: UUID,
        message_id: UUID,
        *,
        user_id: UUID,
        patch: dict[str, Any],
    ) -> bool:
        """Merge *patch* into the message's ``extensions`` JSON (assistant rows only)."""
        if self._db is None:
            return False
        session = await self.get_session(session_id, user_id=user_id)
        if session is None:
            return False

        async with self._db.session() as db:
            sid_eq = await _message_session_sid_eq(db, session_id)
            stmt = select(Message).where(
                uuid_column_equals(db, Message.id, message_id),
                sid_eq,
                Message.role == MessageRole.ASSISTANT,
            )
            result = await db.execute(stmt)
            message = result.scalar_one_or_none()
            if not message:
                return False

            base: dict[str, Any] = {}
            if message.extensions:
                try:
                    parsed = json.loads(message.extensions)
                    if isinstance(parsed, dict):
                        base = parsed
                except json.JSONDecodeError:
                    base = {}
            merged = {**base, **patch}
            extensions = json.dumps(merged, ensure_ascii=False)
            updated_at = utc_now()
            if session_dialect_name(db) == "sqlite":
                await _sqlite_update_message(
                    db,
                    message_id,
                    {"extensions": extensions, "updated_at": updated_at},
                )
            else:
                message.extensions = extensions
                message.updated_at = updated_at

        await self._invalidate_session_message_cache(session_id)

        return True

    async def find_previous_user_message(
        self,
        session_id: UUID,
        *,
        before_created_at: datetime,
        user_id: UUID,
    ) -> Message | None:
        """Latest user message in the session strictly before *before_created_at*."""
        if self._db is None:
            return None
        session = await self.get_session(session_id, user_id=user_id)
        if session is None:
            return None

        async with self._db.session() as db:
            sid_eq = await _message_session_sid_eq(db, session_id)
            stmt = (
                select(Message)
                .where(
                    sid_eq,
                    Message.role == MessageRole.USER,
                    Message.created_at < before_created_at,
                )
                .order_by(col(Message.created_at).desc())
                .limit(1)
            )
            result = await db.execute(stmt)
            return result.scalar_one_or_none()


_chat_service: ChatService | None = None


def get_chat_service() -> ChatService:
    """Get the global chat service instance."""
    if _chat_service is None:
        raise RuntimeError("ChatService not initialized")
    return _chat_service


async def init_chat_service(
    settings: Settings,
    db_service: DatabaseService,
    cache_service: CacheService | None = None,
) -> ChatService:
    """Initialize and start the global chat service."""
    global _chat_service
    _chat_service = ChatService(settings, db_service, cache_service)
    await _chat_service.start()
    return _chat_service
