"""SQLite-specific helpers: UUID text matching, enum coercion, dialect checks.

Centralizes logic used when ``aiosqlite`` stores UUIDs and enums in shapes that
differ from what SQLAlchemy's default loaders expect for PostgreSQL-oriented
schemas.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Protocol, TypeVar
from typing import cast as type_cast
from uuid import UUID

from sqlalchemy import String, false, func, text
from sqlalchemy import cast as sa_cast
from sqlmodel import select

if TYPE_CHECKING:
    from collections.abc import Mapping

    from sqlmodel.ext.asyncio.session import AsyncSession

    from leagent.db.models.file import File, FileStatus, FileType
    from leagent.db.models.message import ChatSession

_PARENT_ID_TABLE_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")

# Tables we may embed in ``SELECT ... FROM {table}`` after validation only.
PARENT_ID_TABLES: frozenset[str] = frozenset(
    {
        "users",
        "workspaces",
        "pet_projects",
        "chat_projects",
        "messages",
        "change_reviews",
        "files",
        "chat_sessions",
        "flows",
        "roles",
        "folders",
        "todos",
        "todo_checklist_items",
        "tasks",
        "notifications",
        "coding_projects",
        "workflow_executions",
        "complaints",
        "knowledge_spaces",
    }
)


class _HasId(Protocol):
    id: Any


_T = TypeVar("_T", bound=_HasId)


async def _scalar_first(session: AsyncSession, stmt: Any) -> Any | None:
    """Run a scalar select against SQLModel or SQLAlchemy async sessions."""
    exec_ = getattr(session, "exec", None)
    if callable(exec_):
        return (await exec_(stmt)).first()
    result = await session.execute(stmt)
    return result.scalars().first()


async def load_entity_by_id(
    session: AsyncSession,
    model_cls: type[_T],
    entity_id: UUID,
    *,
    parent_table: str,
) -> _T | None:
    """ORM ``get`` by primary key with SQLite stored-UUID normalization."""
    if parent_table not in PARENT_ID_TABLES:
        raise ValueError(f"unsupported parent_table for id lookup: {parent_table}")
    if session_dialect_name(session) == "sqlite":
        return await _scalar_first(
            session,
            select(model_cls).where(uuid_column_equals(session, model_cls.id, entity_id)),
        )
    return await session.get(model_cls, entity_id)


def session_dialect_name(session: AsyncSession) -> str:
    """Return ``bind.dialect.name`` (e.g. ``sqlite``, ``postgresql``)."""
    get_bind = getattr(session, "get_bind", None)
    if not callable(get_bind):
        return ""
    bind = get_bind()
    return getattr(getattr(bind, "dialect", None), "name", "") or ""


def parse_uuid_stored(s: str | object) -> UUID:
    """Parse ``id`` values as returned by SQLite ``CAST(... AS TEXT)``."""
    t = str(s).strip()
    if len(t) == 32 and "-" not in t:
        return UUID(hex=t)
    return UUID(t)


def same_user_id(a: object, b: object) -> bool:
    """Compare user ids when SQLite / drivers disagree on UUID vs hex text."""
    try:
        return UUID(str(a)) == UUID(str(b))
    except (ValueError, TypeError, AttributeError):
        return str(a) == str(b)


def _uuid_hex(value: object) -> str:
    try:
        return UUID(str(value)).hex.lower()
    except (ValueError, TypeError, AttributeError):
        return str(value).replace("-", "").lower()


def uuid_column_equals(session: AsyncSession, column: Any, value: object) -> Any:
    """Return a UUID equality predicate tolerant of dashed/hex SQLite text."""
    if value is None:
        return column.is_(None)
    if session_dialect_name(session) == "sqlite":
        normalized_column = func.lower(func.replace(sa_cast(column, String), "-", ""))
        return normalized_column == _uuid_hex(value)
    return column == value


def uuid_columns_equal(session: AsyncSession, left: Any, right: Any) -> Any:
    """Compare two UUID columns while tolerating dashed SQLite text."""
    if session_dialect_name(session) == "sqlite":
        left_normalized = func.lower(func.replace(sa_cast(left, String), "-", ""))
        right_normalized = func.lower(func.replace(sa_cast(right, String), "-", ""))
        return left_normalized == right_normalized
    return left == right


def uuid_column_in(session: AsyncSession, column: Any, values: list[UUID]) -> Any:
    """Return a UUID IN predicate tolerant of dashed/hex SQLite text."""
    if not values:
        return false()
    if session_dialect_name(session) == "sqlite":
        normalized_column = func.lower(func.replace(sa_cast(column, String), "-", ""))
        return normalized_column.in_([_uuid_hex(v) for v in values])
    return column.in_(values)


async def load_user_by_id(session: AsyncSession, user_id: UUID) -> None:
    """No-op — user model removed."""
    return


async def sqlite_user_role_names(session: AsyncSession, user_id: UUID) -> list[str]:
    return ["admin"]


async def sqlite_user_permission_keys(session: AsyncSession, user_id: UUID) -> list[str]:
    return ["*"]


async def load_role_by_id(session: AsyncSession, role_id: UUID) -> None:
    return None


async def load_chat_session_by_id(
    session: AsyncSession,
    chat_session_id: UUID,
    *,
    owner_user_id: UUID | None = None,
) -> ChatSession | None:
    """Load :class:`ChatSession` by PK; optional owner check (SQLite UUID-safe)."""
    from sqlmodel import select

    from leagent.db.models.message import ChatSession

    if session_dialect_name(session) == "sqlite":
        stmt = select(ChatSession).where(uuid_column_equals(session, ChatSession.id, chat_session_id))
        row = await _scalar_first(session, stmt)
        if row is None:
            return None
        if owner_user_id is not None and not same_user_id(row.user_id, owner_user_id):
            return None
        return type_cast("ChatSession", row)
    stmt = select(ChatSession).where(ChatSession.id == chat_session_id)
    if owner_user_id is not None:
        stmt = stmt.where(ChatSession.user_id == owner_user_id)
    row = (await session.execute(stmt)).scalar_one_or_none()
    return type_cast("ChatSession | None", row)


async def sqlite_parent_id_text(session: AsyncSession, table: str, logical: UUID) -> str:
    """Return the exact ``id`` string stored in SQLite for FK child inserts."""
    if table not in PARENT_ID_TABLES or not _PARENT_ID_TABLE_RE.fullmatch(table):
        raise ValueError(f"unsupported table for id lookup: {table}")
    full = str(logical)
    plain = full.replace("-", "").lower()
    result = await session.execute(
        text(
            f"SELECT CAST(id AS TEXT) FROM {table} "
            "WHERE lower(CAST(id AS TEXT)) = lower(:full) "
            "OR lower(replace(CAST(id AS TEXT), '-', '')) = :plain"
        ),
        {"full": full, "plain": plain},
    )
    row = result.first()
    if row is None:
        return full
    return str(row[0])


def _enum_db_value(value: object) -> object:
    """Return enum values in the DB representation used by value-backed enums."""
    enum_value = getattr(value, "value", None)
    return enum_value if enum_value is not None else value


async def sqlite_insert_message(session: AsyncSession, message: Any) -> Any:
    """Insert a ``Message`` row preserving exact SQLite FK UUID text.

    SQLAlchemy's SQLite UUID binder stores UUID objects as 32-char hex strings.
    That is correct for ORM-created rows, but it breaks FK checks when a parent
    row already exists as dashed text (common in raw imports / older local DBs).
    This helper writes FK columns with the exact parent id text while still
    returning an ORM ``Message`` instance.
    """
    from leagent.db.models.message import Message

    session_id = await sqlite_parent_id_text(session, "chat_sessions", message.session_id)
    flow_id = await sqlite_parent_id_text(session, "flows", message.flow_id) if message.flow_id is not None else None
    user_id = await sqlite_parent_id_text(session, "users", message.user_id) if message.user_id is not None else None
    workspace_id = (
        await sqlite_parent_id_text(session, "workspaces", message.workspace_id)
        if message.workspace_id is not None
        else None
    )
    parent_id = (
        await sqlite_parent_id_text(session, "messages", message.parent_id) if message.parent_id is not None else None
    )

    await session.execute(
        text(
            """
            INSERT INTO messages (
                created_at, updated_at, id, role, content, status,
                session_id, flow_id, user_id, workspace_id, model,
                tool_calls, tool_call_id, attachments, extensions,
                input_tokens, output_tokens, total_tokens, latency_ms,
                error, rating, feedback, parent_id
            )
            VALUES (
                :created_at, :updated_at, :id, :role, :content, :status,
                :session_id, :flow_id, :user_id, :workspace_id, :model,
                :tool_calls, :tool_call_id, :attachments, :extensions,
                :input_tokens, :output_tokens, :total_tokens, :latency_ms,
                :error, :rating, :feedback, :parent_id
            )
            """
        ),
        {
            "created_at": message.created_at,
            "updated_at": message.updated_at,
            "id": message.id.hex,
            "role": _enum_db_value(message.role),
            "content": message.content,
            "status": _enum_db_value(message.status),
            "session_id": session_id,
            "flow_id": flow_id,
            "user_id": user_id,
            "workspace_id": workspace_id,
            "model": message.model,
            "tool_calls": message.tool_calls,
            "tool_call_id": message.tool_call_id,
            "attachments": message.attachments,
            "extensions": message.extensions,
            "input_tokens": message.input_tokens,
            "output_tokens": message.output_tokens,
            "total_tokens": message.total_tokens,
            "latency_ms": message.latency_ms,
            "error": message.error,
            "rating": message.rating,
            "feedback": message.feedback,
            "parent_id": parent_id,
        },
    )
    result = await session.exec(select(Message).where(uuid_column_equals(session, Message.id, message.id)))
    row = result.first()
    if row is None:
        raise RuntimeError("Inserted message row could not be reloaded")
    return row


def file_type_from_db(raw: object) -> FileType:
    """Map DB ``file_type`` (``IMAGE`` or ``image``) to :class:`FileType`."""
    from leagent.db.models.file import FileType

    s = str(raw).strip()
    if s in FileType.__members__:
        return FileType[s]
    low = s.lower()
    for ft in FileType:
        if ft.value == low:
            return ft
    return FileType.OTHER


def file_status_from_db(raw: object) -> FileStatus:
    """Map DB ``status`` (``PROCESSED`` or ``processed``) to :class:`FileStatus`."""
    from leagent.db.models.file import FileStatus

    s = str(raw).strip()
    if s in FileStatus.__members__:
        return FileStatus[s]
    low = s.lower()
    for st in FileStatus:
        if st.value == low:
            return st
    return FileStatus.UPLOADED


def file_model_from_sqlite_row(row: Mapping[str, Any]) -> File:
    """Hydrate :class:`File` from a raw SQLite row (avoids broken enum coercion)."""
    from leagent.db.models.file import File as FileModel
    from leagent.db.models.file import LibraryScope
    from leagent.db.models.knowledge_acl import KnowledgeAccessScope

    d = dict(row)
    return FileModel(
        id=parse_uuid_stored(str(d["id"])),
        created_at=d["created_at"],
        updated_at=d["updated_at"],
        is_deleted=bool(d.get("is_deleted", False)),
        deleted_at=d.get("deleted_at"),
        name=d["name"],
        original_name=d["original_name"],
        file_type=file_type_from_db(d["file_type"]),
        mime_type=d.get("mime_type"),
        size=int(d["size"]),
        status=file_status_from_db(d["status"]),
        user_id=parse_uuid_stored(str(d["user_id"])) if d.get("user_id") is not None else None,
        workspace_id=(parse_uuid_stored(str(d["workspace_id"])) if d.get("workspace_id") is not None else None),
        knowledge_space_id=(
            parse_uuid_stored(str(d["knowledge_space_id"])) if d.get("knowledge_space_id") is not None else None
        ),
        access_scope=KnowledgeAccessScope(str(d.get("access_scope") or KnowledgeAccessScope.PRIVATE.value)),
        folder_id=(parse_uuid_stored(str(d["folder_id"])) if d.get("folder_id") is not None else None),
        session_id=(parse_uuid_stored(str(d["session_id"])) if d.get("session_id") is not None else None),
        storage_path=d["storage_path"],
        storage_bucket=d.get("storage_bucket"),
        checksum=d.get("checksum"),
        extracted_text=d.get("extracted_text"),
        file_metadata=d.get("file_metadata"),
        page_count=d.get("page_count"),
        has_ocr=bool(d.get("has_ocr", False)),
        ocr_language=d.get("ocr_language"),
        embedding_id=d.get("embedding_id"),
        is_indexed=bool(d.get("is_indexed", False)),
        expires_at=d.get("expires_at"),
        library_scope=LibraryScope(str(d.get("library_scope") or LibraryScope.WORKSPACE.value)),
    )


def workspace_member_role_from_db(raw: object) -> str:
    return str(raw).strip().lower()


__all__ = [
    "PARENT_ID_TABLES",
    "session_dialect_name",
    "parse_uuid_stored",
    "same_user_id",
    "sqlite_insert_message",
    "uuid_column_equals",
    "uuid_column_in",
    "sqlite_parent_id_text",
    "load_user_by_id",
    "load_role_by_id",
    "load_chat_session_by_id",
    "load_entity_by_id",
    "sqlite_user_role_names",
    "sqlite_user_permission_keys",
    "file_type_from_db",
    "file_status_from_db",
    "file_model_from_sqlite_row",
    "workspace_member_role_from_db",
]
