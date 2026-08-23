"""Tiered persistence backend for :class:`SessionState`.

Two-tier store: in-process LRU (L1) + database (durable).

**Single source of truth (SSOT).** The durable session state is the
``session_state_v1`` JSON blob stored on ``chat_sessions.session_metadata``.
:meth:`TieredSessionStore.load` materialises from that blob first and only
rehydrates from the ``messages`` table when the blob is absent (legacy rows).

The ``messages`` table is a **projection** for UI history queries, not the
authoritative transcript:

* User and assistant rows are written exactly once by
  ``ChatService.add_message`` (from the HTTP/stream handler) — the store never
  re-inserts them, eliminating the historical double-write.
* Only tool/system projection rows (which ``ChatService`` does not own) are
  written here, and only when missing (keyed on the stable message UUID).

The LRU is a read-through cache; it never holds state the JSON blob does not.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import text as sa_text
from sqlmodel import col, select

from StAgent.db.models.base import naive_utc_for_db_column
from StAgent.db.models.identity_stub import UserStub, WorkspaceStub
from StAgent.db.models.message import ChatSession, Message, MessageRole
from StAgent.db.sqlite_compat import (
    load_chat_session_by_id,
    parse_uuid_stored,
    session_dialect_name,
    sqlite_insert_message,
    sqlite_parent_id_text,
    uuid_column_equals,
)
from StAgent.services.cache.lru import BoundedLRU
from StAgent.services.session.state import (
    SessionAttachment,
    SessionMessage,
    SessionState,
    SessionUsage,
)
from StAgent.utils.logging import get_logger

if TYPE_CHECKING:
    from StAgent.config.settings import Settings
    from StAgent.db.service import DatabaseService
    from StAgent.services.cache.service import CacheService

logger = get_logger(__name__)


def _as_utc_aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _as_utc_naive_for_db(dt: datetime | None) -> datetime | None:
    """UTC as naive datetime for ``DateTime`` columns without time zone."""
    return naive_utc_for_db_column(dt)


def _normalize_chat_session_timestamps(chat: ChatSession) -> None:
    """Coerce `ChatSession` ORM fields to naive UTC (matches :class:`TimestampMixin`)."""
    if chat.created_at is not None:
        c = _as_utc_naive_for_db(chat.created_at)
        if c is not None:
            chat.created_at = c
    if chat.updated_at is not None:
        u = _as_utc_naive_for_db(chat.updated_at)
        if u is not None:
            chat.updated_at = u
    if chat.last_message_at is not None:
        lm = _as_utc_naive_for_db(chat.last_message_at)
        if lm is not None:
            chat.last_message_at = lm


_SESSION_METADATA_KEY = "session_state_v1"
_SESSION_CACHE_NAMESPACE = "sessions"


def _role_to_enum(role: str) -> MessageRole:
    try:
        return MessageRole(role)
    except ValueError:
        return MessageRole.USER


class _LRUCache:
    """Tiny async-friendly LRU for hot-path session reads.

    Backed by the shared thread-safe :class:`BoundedLRU` primitive so eviction
    accounting and concurrency semantics match the rest of the cache stack.
    """

    def __init__(self, max_size: int) -> None:
        self._lru = BoundedLRU(max(8, max_size))

    def get(self, session_id: UUID) -> SessionState | None:
        value, hit = self._lru.get(str(session_id))
        return value if hit else None

    def put(self, state: SessionState) -> None:
        self._lru.set(str(state.session_id), state)

    def drop(self, session_id: UUID) -> None:
        self._lru.delete(str(session_id))


class TieredSessionStore:
    """Two-tier read/write backend for :class:`SessionState`.

    Reads consult the in-process LRU first, then the database.
    Writes go to both tiers.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        database: DatabaseService | None,
        cache: CacheService | None = None,
    ) -> None:
        self._settings = settings
        self._database = database
        self._cache = cache
        self._lru = _LRUCache(settings.session.in_memory_lru_size)
        # session_id str → sha256 of the last durably-saved snapshot JSON.
        self._saved_digests = BoundedLRU(512)

    # -- public API -----------------------------------------------------

    async def load(self, session_id: UUID) -> SessionState | None:
        cached = self._lru.get(session_id)
        if cached is not None:
            return cached

        state = await self._load_from_cache(session_id)
        if state is not None:
            self._lru.put(state)
            return state

        state = await self._load_from_database(session_id)
        if state is not None:
            self._lru.put(state)
            await self._save_to_cache(state)
        return state

    async def save(self, state: SessionState) -> None:
        # Persist to the durable tier first; only publish to the read-through
        # caches after the database write succeeds so a failed save can never
        # leave an undurable state visible to subsequent reads.
        snapshot_json = json.dumps(
            {_SESSION_METADATA_KEY: state.to_dict()},
            ensure_ascii=False,
        )
        digest: str | None = None
        if self._database is not None:
            # ``locked()`` exits and ``get_or_create`` re-save unchanged
            # states; skip the blob UPDATE + message-existence scan when the
            # serialized snapshot is byte-identical to the last successful
            # write. The digest only advances after a fully durable save, so
            # a failed save is always retried.
            digest = hashlib.sha256(snapshot_json.encode("utf-8")).hexdigest()
            prev, hit = self._saved_digests.get(str(state.session_id))
            if hit and prev == digest:
                self._lru.put(state)
                return
        await self._save_to_database(state, snapshot_json)
        self._lru.put(state)
        await self._save_to_cache(state)
        if digest is not None:
            self._saved_digests.set(str(state.session_id), digest)

    async def delete(self, session_id: UUID) -> None:
        self._lru.drop(session_id)
        # A stale digest could suppress the first save of a recreated session
        # with identical content.
        self._saved_digests.delete(str(session_id))
        await self._delete_from_cache(session_id)

    # -- cache (process-local CacheService L2) -------------------------

    @staticmethod
    def _cache_key(session_id: UUID) -> str:
        return f"state:{session_id}"

    async def _load_from_cache(self, session_id: UUID) -> SessionState | None:
        if self._cache is None:
            return None
        key = self._cache_key(session_id)
        try:
            cached = await self._cache.get(key, namespace=_SESSION_CACHE_NAMESPACE)
        except Exception as exc:  # noqa: BLE001
            logger.debug("session_store_cache_load_failed: %s", exc)
            return None
        if cached is None:
            return None
        try:
            if isinstance(cached, SessionState):
                return cached
            if isinstance(cached, dict):
                return SessionState.from_dict(cached)
        except Exception as exc:  # noqa: BLE001
            logger.warning("session_store_cache_decode_failed: %s", exc)
            await self._delete_from_cache(session_id)
        return None

    async def _save_to_cache(self, state: SessionState) -> None:
        if self._cache is None:
            return
        try:
            await self._cache.set(
                self._cache_key(state.session_id),
                state.to_dict(),
                namespace=_SESSION_CACHE_NAMESPACE,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("session_store_cache_save_failed: %s", exc)

    async def _delete_from_cache(self, session_id: UUID) -> None:
        if self._cache is None:
            return
        try:
            await self._cache.delete(
                self._cache_key(session_id),
                namespace=_SESSION_CACHE_NAMESPACE,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("session_store_cache_delete_failed: %s", exc)

    # -- database (SQLite / Postgres) -----------------------------------

    async def _load_from_database(self, session_id: UUID) -> SessionState | None:
        if self._database is None:
            return None
        try:
            async with self._database.session() as db:
                chat = await load_chat_session_by_id(
                    db, session_id, owner_user_id=None
                )
                if chat is None:
                    return None
                state = self._materialise_from_chat_session(chat)
                if state is None:
                    state = await self._rehydrate_from_messages(db, chat)
                return state
        except Exception as exc:  # noqa: BLE001
            logger.warning("session_store_database_load_failed: %s", exc)
            return None

    def _materialise_from_chat_session(
        self, chat: ChatSession
    ) -> SessionState | None:
        """Read the stashed JSON blob from ``chat_sessions.session_metadata``."""
        raw = chat.session_metadata
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return None
        snapshot = data.get(_SESSION_METADATA_KEY) if isinstance(data, dict) else None
        if not snapshot:
            return None
        try:
            return SessionState.from_dict(snapshot)
        except Exception as exc:  # noqa: BLE001
            logger.warning("session_store_snapshot_decode_failed: %s", exc)
            return None

    async def _rehydrate_from_messages(
        self,
        db: Any,
        chat: ChatSession,
    ) -> SessionState:
        """Fallback: rebuild the transcript from the ``messages`` table."""
        if session_dialect_name(db) == "sqlite":
            sid_eq = uuid_column_equals(db, Message.session_id, chat.id)
        else:
            sid_eq = Message.session_id == chat.id
        result = await db.exec(
            select(Message).where(sid_eq).order_by(Message.created_at.asc(), Message.id.asc())
        )
        rows = list(result.all())

        messages: list[SessionMessage] = []
        for row in rows:
            tool_calls: list[dict[str, Any]] | None = None
            if row.tool_calls:
                try:
                    tool_calls = json.loads(row.tool_calls)
                except (TypeError, ValueError):
                    tool_calls = None
            attachment_ids: list[str] = []
            if row.attachments:
                try:
                    attachment_ids = [str(a) for a in json.loads(row.attachments) or []]
                except (TypeError, ValueError):
                    attachment_ids = []
            reasoning_content: str | None = None
            if row.extensions:
                try:
                    ext = json.loads(row.extensions)
                except (TypeError, ValueError):
                    ext = None
                if isinstance(ext, dict):
                    rc = ext.get("reasoning_content")
                    if isinstance(rc, str) and rc.strip():
                        reasoning_content = rc
            messages.append(
                SessionMessage(
                    id=row.id,
                    role=row.role.value if hasattr(row.role, "value") else str(row.role),
                    content=row.content,
                    created_at=_as_utc_aware(row.created_at) or datetime.now(UTC),
                    tool_calls=tool_calls,
                    tool_call_id=row.tool_call_id,
                    attachment_ids=attachment_ids,
                    model=row.model,
                    reasoning_content=reasoning_content,
                )
            )

        usage = SessionUsage(
            input_tokens=sum((r.input_tokens or 0) for r in rows),
            output_tokens=sum((r.output_tokens or 0) for r in rows),
            total_tokens=sum((r.total_tokens or 0) for r in rows),
            turns=sum(1 for r in rows if r.role == MessageRole.ASSISTANT),
        )

        return SessionState(
            session_id=chat.id,
            user_id=chat.user_id,
            workspace_id=chat.workspace_id,
            flow_id=chat.flow_id,
            messages=messages,
            attachments=[],
            file_state=[],
            usage=usage,
            created_at=_as_utc_aware(chat.created_at) or datetime.now(UTC),
            updated_at=_as_utc_aware(chat.last_message_at or chat.updated_at)
            or datetime.now(UTC),
        )

    async def _save_to_database(self, state: SessionState, snapshot_json: str) -> None:
        if self._database is None:
            return
        now_db = _as_utc_naive_for_db(datetime.now(UTC))
        last_at = _as_utc_naive_for_db(_as_utc_aware(state.updated_at)) or now_db
        try:
            async with self._database.session() as db:
                sqlite = session_dialect_name(db) == "sqlite"
                chat = await load_chat_session_by_id(
                    db, state.session_id, owner_user_id=None
                )
                if chat is None:
                    # ``chat_sessions.user_id`` is NOT NULL. Channel / background
                    # turns occasionally omit it; fall back to the local owner
                    # rather than failing the whole save with IntegrityError.
                    owner_id = state.user_id
                    if owner_id is None:
                        from StAgent.config.constants import LOCAL_USER_ID

                        owner_id = LOCAL_USER_ID
                        state.user_id = owner_id
                    # ``chat_sessions.user_id`` and the mirrored message rows
                    # have real foreign keys.  Background/channel callers may
                    # provide an owner id before the auth layer has materialised
                    # its identity row, so create the minimal parent stubs before
                    # the raw SQLite insert (and do the same for workspaces).
                    identity_rows = []
                    if await db.get(UserStub, owner_id) is None:
                        identity_rows.append(UserStub(id=owner_id))
                    if (
                        state.workspace_id is not None
                        and await db.get(WorkspaceStub, state.workspace_id) is None
                    ):
                        identity_rows.append(WorkspaceStub(id=state.workspace_id))
                    if identity_rows:
                        db.add_all(identity_rows)
                        await db.flush()
                    if sqlite:
                        created_at = (
                            _as_utc_naive_for_db(_as_utc_aware(state.created_at))
                            or now_db
                        )
                        await db.execute(
                            sa_text(
                                """
                                INSERT INTO chat_sessions (
                                    created_at, updated_at, id, name, user_id,
                                    flow_id, project_id, workspace_id, is_active,
                                    message_count, last_message_at, session_metadata
                                )
                                VALUES (
                                    :created_at, :updated_at, :id, NULL, :user_id,
                                    :flow_id, NULL, :workspace_id, 1,
                                    :message_count, :last_message_at, :session_metadata
                                )
                                """
                            ),
                            {
                                "created_at": created_at,
                                "updated_at": now_db,
                                "id": state.session_id.hex,
                                "user_id": await sqlite_parent_id_text(
                                    db, "users", owner_id
                                ),
                                "flow_id": (
                                    await sqlite_parent_id_text(
                                        db, "flows", state.flow_id
                                    )
                                    if state.flow_id is not None
                                    else None
                                ),
                                "workspace_id": (
                                    await sqlite_parent_id_text(
                                        db, "workspaces", state.workspace_id
                                    )
                                    if state.workspace_id is not None
                                    else None
                                ),
                                "message_count": len(state.messages),
                                "last_message_at": last_at,
                                "session_metadata": snapshot_json,
                            },
                        )
                    else:
                        chat = ChatSession(
                            id=state.session_id,
                            user_id=owner_id,
                            flow_id=state.flow_id,
                            workspace_id=state.workspace_id,
                            message_count=len(state.messages),
                            last_message_at=last_at,
                            session_metadata=snapshot_json,
                            updated_at=now_db,
                        )
                        db.add(chat)
                elif sqlite:
                    await db.execute(
                        sa_text(
                            """
                            UPDATE chat_sessions
                            SET message_count = :message_count,
                                last_message_at = :last_message_at,
                                session_metadata = :session_metadata,
                                updated_at = :updated_at
                            WHERE lower(replace(CAST(id AS VARCHAR), '-', '')) = :session_id
                            """
                        ),
                        {
                            "message_count": len(state.messages),
                            "last_message_at": last_at,
                            "session_metadata": snapshot_json,
                            "updated_at": now_db,
                            "session_id": str(state.session_id).replace("-", "").lower(),
                        },
                    )
                else:
                    _normalize_chat_session_timestamps(chat)
                    chat.message_count = len(state.messages)
                    chat.last_message_at = last_at
                    chat.session_metadata = snapshot_json
                    chat.updated_at = now_db

                # Persist only messages that don't already exist. We rely on
                # the message's primary key (UUID) to avoid duplicates —
                # ``SessionMessage.id`` is stable per message.
                if session_dialect_name(db) == "sqlite":
                    sid_eq = uuid_column_equals(db, Message.session_id, state.session_id)
                else:
                    sid_eq = Message.session_id == state.session_id
                existing_ids = await db.exec(select(Message.id).where(sid_eq))
                # ``exec(select(Model.id))`` may yield scalars (UUID) or
                # one-column Row tuples depending on SQLAlchemy/SQLModel version.
                existing = set()
                for r in existing_ids.all():
                    raw_id = r if isinstance(r, (UUID, str)) else r[0]
                    existing.add(parse_uuid_stored(str(raw_id)))

                for msg in state.messages:
                    mid = parse_uuid_stored(str(msg.id))
                    if mid in existing:
                        continue
                    role_enum = _role_to_enum(msg.role)
                    # User and assistant rows are persisted by :class:`ChatService`
                    # (``add_message`` in the HTTP/stream handler) before the agent
                    # runs.  Re-inserting them here would create duplicates whenever
                    # the controller assigns a new UUID due to index drift after
                    # trim / stub-injection.  The full transcript (including these
                    # roles) is already durable in the ``session_state_v1`` JSON
                    # blob written above; the ``messages`` table mirrors are for UI
                    # history queries only.
                    if role_enum in (MessageRole.ASSISTANT, MessageRole.USER):
                        continue
                    row = Message(
                        id=msg.id,
                        session_id=state.session_id,
                        flow_id=state.flow_id,
                        user_id=state.user_id,
                        workspace_id=state.workspace_id,
                        role=_role_to_enum(msg.role),
                        content=msg.content,
                        model=msg.model,
                        tool_calls=(
                            json.dumps(msg.tool_calls) if msg.tool_calls else None
                        ),
                        tool_call_id=msg.tool_call_id,
                        attachments=(
                            json.dumps(msg.attachment_ids)
                            if msg.attachment_ids
                            else None
                        ),
                        created_at=_as_utc_naive_for_db(
                            _as_utc_aware(msg.created_at)
                        )
                        or now_db,
                    )
                    if session_dialect_name(db) == "sqlite":
                        await sqlite_insert_message(db, row)
                    else:
                        db.add(row)
        except Exception as exc:  # noqa: BLE001
            # Re-raise after logging so ``save`` cannot publish an undurable
            # state to L1. The runtime recovery boundary in ``SessionManager``
            # catches this and leaves the previously durable state untouched.
            logger.warning("session_store_database_save_failed: %s", exc)
            raise

    async def list_sessions_for_user(
        self,
        user_id: UUID,
        *,
        limit: int = 50,
    ) -> list[SessionState]:
        """Return the most recently updated sessions for a user."""
        if self._database is None:
            return []
        try:
            async with self._database.session() as db:
                # Single index-backed query; previously ids were fetched then
                # each session re-loaded individually (N+1).
                result = await db.exec(
                    select(ChatSession)
                    .where(uuid_column_equals(db, ChatSession.user_id, user_id))
                    .where(col(ChatSession.is_active).is_(True))
                    .order_by(
                        col(ChatSession.last_message_at).desc().nulls_last(),
                        col(ChatSession.updated_at).desc(),
                    )
                    .limit(limit)
                )
                rows = list(result.all())
        except Exception as exc:  # noqa: BLE001
            logger.warning("session_store_list_failed: %s", exc)
            return []

        states: list[SessionState] = []
        for chat in rows:
            materialised = self._materialise_from_chat_session(chat)
            if materialised is None:
                materialised = SessionState(
                    session_id=chat.id,
                    user_id=chat.user_id,
                    workspace_id=chat.workspace_id,
                    flow_id=chat.flow_id,
                    created_at=_as_utc_aware(chat.created_at) or datetime.now(UTC),
                    updated_at=_as_utc_aware(chat.last_message_at or chat.updated_at)
                    or datetime.now(UTC),
                )
            states.append(materialised)
        return states

    # -- introspection --------------------------------------------------

    def cached_attachments(self, session_id: UUID) -> list[SessionAttachment]:
        state = self._lru.get(session_id)
        if state is None:
            return []
        return list(state.attachments)
