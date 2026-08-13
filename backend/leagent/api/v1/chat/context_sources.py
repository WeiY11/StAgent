"""Resolve chat context sources into agent-readable storage paths.

A chat request can carry several kinds of context pointers — a selected UI
folder, explicit ``file_ids``, a coding-project folder, session-level authorized
roots, and inline ``@knowledge:`` mentions. This module resolves each of them
(always user-scoped) into concrete storage paths and prompt notes the agent can
consume, never raising on bad pointers (chat degrades gracefully instead).
"""

from __future__ import annotations

import re
import tempfile
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from leagent.api.v1.chat.paths import dedupe_resolved_paths

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from uuid import UUID

    from leagent.db import DatabaseService
    from leagent.services.chat.service import ChatService

logger = structlog.get_logger(__name__)


async def resolve_folder_context(
    user_id: UUID,
    db: DatabaseService,
    folder_id: str | None = None,
    file_ids_csv: str | None = None,
) -> list[tuple[str, str, str | None]]:
    """Fetch ``(storage_path, name, preview)`` for a folder and/or explicit file ids."""
    rows = await _resolve_authorized_folder_context_rows(
        user_id,
        db,
        folder_id=folder_id,
        file_ids_csv=file_ids_csv,
    )
    results: list[tuple[str, str, str | None]] = []
    for row in rows:
        preview = (row.extracted_text or "")[:1500] if row.extracted_text else None
        results.append((row.storage_path, row.original_name, preview))
    return results


async def _resolve_authorized_folder_context_rows(
    user_id: UUID,
    db: DatabaseService,
    *,
    folder_id: str | None = None,
    file_ids_csv: str | None = None,
) -> list[Any]:
    """Return file rows only after folder/file-id ownership filters apply."""
    from uuid import UUID as _UUID

    from sqlmodel import select as sel

    from leagent.db.models.file import File as FileModel
    from leagent.db.sqlite_compat import uuid_column_equals, uuid_column_in

    rows_out: list[Any] = []
    raw_ids: list[_UUID] = []
    if file_ids_csv:
        for part in file_ids_csv.split(","):
            part = part.strip()
            if part:
                with suppress(ValueError):
                    raw_ids.append(_UUID(part))

    async with db.session() as session:
        if folder_id:
            try:
                fid = _UUID(folder_id)
            except ValueError:
                fid = None
            if fid:
                stmt = sel(FileModel).where(
                    uuid_column_equals(session, FileModel.folder_id, fid),
                    uuid_column_equals(session, FileModel.user_id, user_id),
                    FileModel.is_deleted == False,  # noqa: E712
                )
                rows = (await session.exec(stmt)).all()
                rows_out.extend(rows)

        if raw_ids:
            stmt = sel(FileModel).where(
                uuid_column_in(session, FileModel.id, raw_ids),
                uuid_column_equals(session, FileModel.user_id, user_id),
                FileModel.is_deleted == False,  # noqa: E712
            )
            rows = (await session.exec(stmt)).all()
            rows_out.extend(rows)

    return rows_out


def context_item_paths(
    context_items: list[tuple[str, str, str | None]],
) -> list[str]:
    """Project the storage-path column out of :func:`resolve_folder_context` rows."""
    return [path for path, _name, _preview in context_items]


async def resolve_folder_context_note(
    user_id: UUID,
    db: DatabaseService,
    folder_id: str | None,
    *,
    attached_file_count: int,
) -> str | None:
    """Return a short prompt note naming the selected UI folder (with its path)."""
    from uuid import UUID as _UUID

    if not folder_id or not str(folder_id).strip():
        return None
    try:
        fid = _UUID(str(folder_id))
    except ValueError:
        return None

    from sqlmodel import select

    from leagent.db.models import Folder
    from leagent.db.sqlite_compat import uuid_column_equals

    async with db.session() as session:
        folder = (
            await session.exec(
                select(Folder).where(
                    uuid_column_equals(session, Folder.id, fid),
                    uuid_column_equals(session, Folder.user_id, user_id),
                    Folder.is_deleted == False,  # noqa: E712
                ),
            )
        ).first()
        if not folder:
            return None

        names = [folder.name]
        parent_id = folder.parent_id
        seen = {folder.id}
        while parent_id and parent_id not in seen:
            seen.add(parent_id)
            parent = (
                await session.exec(
                    select(Folder).where(
                        uuid_column_equals(session, Folder.id, parent_id),
                        uuid_column_equals(session, Folder.user_id, user_id),
                        Folder.is_deleted == False,  # noqa: E712
                    ),
                )
            ).first()
            if not parent:
                break
            names.insert(0, parent.name)
            parent_id = parent.parent_id

    folder_path = " / ".join(name for name in names if name)
    lines = [
        "\n\nSelected folder context:",
        f"- Folder: {folder_path or str(fid)}",
        f"- Folder ID: {fid}",
        f"- Attached files from this folder: {attached_file_count}",
        'When the user says "this folder", interpret it as this selected folder.',
    ]
    return "\n".join(lines)


def chat_project_workspace_note(
    *,
    folder_id: UUID | str,
    files_root: str,
    attached_file_count: int = 0,
) -> str:
    """Prompt note for a chat-project shared file space (all sessions share it)."""
    lines = [
        "\n\nShared project workspace:",
        f"- Catalog folder ID: {folder_id}",
        f"- On-disk files root: {files_root}",
        f"- Attached files from this project folder: {attached_file_count}",
        "All conversations in this chat project share this workspace.",
        "Prefer reading and writing under the project files root; do not invent a per-session silo.",
    ]
    return "\n".join(lines)


async def resolve_project_folder_path(
    user_id: UUID,
    db: DatabaseService,
    project_folder_id: str | None,
) -> str | None:
    """Resolve a folder id to its on-disk ``project_path`` (or ``None``).

    Returns ``None`` silently when the id is empty / malformed, the folder
    doesn't exist, the caller doesn't own it, or project mode is off — chat
    should not 4xx for an invalid project pointer; the LLM just runs without
    project context and tools refuse on their own if asked to touch a forbidden
    path.
    """
    from uuid import UUID as _UUID

    if not project_folder_id or not str(project_folder_id).strip():
        return None
    try:
        fid = _UUID(str(project_folder_id))
    except ValueError:
        return None

    from leagent.db.models import Folder
    from leagent.db.sqlite_compat import load_entity_by_id, same_user_id
    from leagent.project.paths import (
        ProjectPathSafetyError,
        resolve_owned_project_folder,
    )

    async with db.session() as session:
        folder = await load_entity_by_id(session, Folder, fid, parent_table="folders")
        if not folder or folder.is_deleted or not same_user_id(folder.user_id, user_id):
            return None
        try:
            resolved = resolve_owned_project_folder(folder, user_id)
        except ProjectPathSafetyError as exc:
            logger.info(
                "chat_project_folder_rejected",
                folder_id=str(fid),
                error=str(exc),
            )
            return None
    return str(resolved)


async def authorized_root_paths_for_session(
    chat_svc: ChatService,
    session_id: UUID,
    user_id: UUID,
) -> list[str] | None:
    """Resolved directory paths from a session's ``authorized_roots`` metadata."""
    items = await chat_svc.list_authorized_roots(session_id, user_id=user_id)
    paths: list[str] = []
    for it in items:
        if isinstance(it, dict):
            p = it.get("path")
            if isinstance(p, str) and p.strip():
                paths.append(p.strip())
    deduped = dedupe_resolved_paths(paths)
    return deduped if deduped else None


_KNOWLEDGE_LINE_RE = re.compile(r"@knowledge:([^\n]+)", re.UNICODE)
_UUID_36_RE = re.compile(r"^[0-9a-fA-F-]{36}$", re.IGNORECASE)
_S3_STORAGE_PATH_RE = re.compile(r"^s3://", re.IGNORECASE)
_SAFE_TEMP_SUFFIX_RE = re.compile(r"^\.[A-Za-z0-9]{1,16}$")


def _is_s3_storage_path(storage_path: str) -> bool:
    """Return whether *storage_path* is an S3 URI, never a filesystem path."""
    return bool(_S3_STORAGE_PATH_RE.match(storage_path or ""))


def parse_knowledge_line_payload(raw: str) -> tuple[UUID | None, str | None]:
    """Return ``(file_id, original_name)`` from the text after ``@knowledge:``.

    If ``#<uuid>`` is present, the name is the segment before the last ``#`` and
    the tail must be a valid UUID; otherwise the whole string is a display name.
    """
    from uuid import UUID as _UUID

    s = raw.strip()
    if not s:
        return None, None
    if "#" in s:
        name, tail = s.rsplit("#", 1)
        tid = tail.strip()
        if _UUID_36_RE.match(tid):
            with suppress(ValueError):
                return _UUID(tid), (name.strip() or None)
    return None, s or None


async def resolve_knowledge_message_paths(
    user_id: UUID,
    db: DatabaseService,
    message: str,
) -> list[str]:
    """Resolve ``@knowledge:…`` mentions in *message* to indexed document paths.

    When ``#<file_uuid>`` is omitted, matches :attr:`File.original_name` for the
    user (``is_deleted == False``); if multiple rows match, the newest is used
    and a warning is logged.
    """
    out: list[str] = []
    for row in await _resolve_authorized_knowledge_rows(user_id, db, message):
        storage_path = str(getattr(row, "storage_path", "") or "")
        if not storage_path or _is_s3_storage_path(storage_path):
            continue
        with suppress(OSError, RuntimeError, ValueError):
            out.append(str(Path(storage_path).expanduser().resolve()))
    return out


async def _resolve_authorized_knowledge_rows(
    user_id: UUID,
    db: DatabaseService,
    message: str,
) -> list[Any]:
    """Return knowledge catalogue rows only after applying the read ACL."""
    from sqlmodel import col, select

    from leagent.db.models.file import File as FileModel
    from leagent.db.sqlite_compat import uuid_column_equals
    from leagent.knowledge.acl import knowledge_file_access_clause

    refs: list[tuple[UUID | None, str | None]] = []
    for match in _KNOWLEDGE_LINE_RE.finditer(message or ""):
        file_id, name = parse_knowledge_line_payload(match.group(1) or "")
        if file_id is not None or (name and name.strip()):
            refs.append((file_id, name))
    if not refs:
        return []

    rows_out: list[Any] = []
    async with db.session() as session:
        for file_id, name in refs:
            row = None
            if file_id is not None:
                stmt = select(FileModel).where(
                    uuid_column_equals(session, FileModel.id, file_id),
                    knowledge_file_access_clause(session, user_id),
                    FileModel.is_deleted == False,  # noqa: E712
                )
                row = (await session.exec(stmt)).first()
            elif name:
                stmt = (
                    select(FileModel)
                    .where(
                        knowledge_file_access_clause(session, user_id),
                        FileModel.is_deleted == False,  # noqa: E712
                        FileModel.original_name == name,
                    )
                    .order_by(col(FileModel.created_at).desc())
                )
                rows = list((await session.exec(stmt)).all())
                if len(rows) > 1:
                    logger.warning(
                        "knowledge_ref_ambiguous_name: user_id=%s name=%r count=%s",
                        user_id,
                        name,
                        len(rows),
                    )
                row = rows[0] if rows else None
            if row and row.storage_path:
                rows_out.append(row)
    return rows_out


def _configured_file_service() -> Any | None:
    """Return the active FileService without constructing a local fallback."""
    try:
        from leagent.services.service_manager import get_service_manager

        return getattr(get_service_manager(), "file_service", None)
    except Exception:  # noqa: BLE001 - local paths remain usable without it
        return None


def _storage_library_scope(row: Any) -> str:
    scope = getattr(row, "library_scope", None)
    return str(getattr(scope, "value", scope) or "knowledge")


def _safe_staging_suffix(row: Any) -> str:
    filename = str(getattr(row, "original_name", "") or getattr(row, "name", ""))
    suffix = Path(filename).suffix
    return suffix if _SAFE_TEMP_SUFFIX_RE.fullmatch(suffix) else ""


async def _stage_s3_authorized_file_row(
    row: Any,
    *,
    file_service: Any,
    staged_paths: list[Path],
) -> str | None:
    """Download one already-authorised S3 row into a turn-scoped temp file."""
    storage_path = str(getattr(row, "storage_path", "") or "")
    staged_path: Path | None = None
    try:
        ref = file_service.ref_from_persisted_record(
            file_id=row.id,
            filename=str(getattr(row, "name", "") or getattr(row, "original_name", "")),
            storage_path=storage_path,
            content_type=getattr(row, "mime_type", None),
            size=getattr(row, "size", None),
            checksum=getattr(row, "checksum", None),
            user_id=getattr(row, "user_id", None),
            session_id=getattr(row, "session_id", None),
            library_scope=_storage_library_scope(row),
        )
        raw, _content_type = await file_service.download(ref)
        if not isinstance(raw, bytes):
            raise TypeError("knowledge object storage returned non-bytes content")
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix="leagent-context-",
            suffix=_safe_staging_suffix(row),
            delete=False,
        ) as staged:
            staged_path = Path(staged.name)
            staged.write(raw)
        staged_paths.append(staged_path)
        return str(staged_path.resolve())
    except Exception:  # noqa: BLE001 - unavailable remote documents degrade chat safely
        if staged_path is not None:
            with suppress(OSError):
                staged_path.unlink()
        logger.warning(
            "chat_context_s3_materialization_failed",
            file_id=str(getattr(row, "id", "")),
            exc_info=True,
        )
        return None


@asynccontextmanager
async def _materialize_authorized_file_rows(
    rows: list[Any],
    *,
    file_service: Any | None = None,
) -> AsyncIterator[list[str]]:
    """Yield paths from already-authorised catalogue rows for one turn.

    Callers must obtain *rows* from an ownership/ACL-scoped query; this helper
    intentionally never accepts a raw storage URI.  The ``finally`` block
    removes every temporary file after agent completion, generator shutdown,
    disconnect, or exception.
    """
    staged_paths: list[Path] = []
    try:
        paths: list[str] = []
        service = file_service
        seen_file_ids: set[str] = set()
        for row in rows:
            file_id = str(getattr(row, "id", "") or "")
            if file_id and file_id in seen_file_ids:
                continue
            if file_id:
                seen_file_ids.add(file_id)

            storage_path = str(getattr(row, "storage_path", "") or "")
            if not storage_path:
                continue
            if not _is_s3_storage_path(storage_path):
                with suppress(OSError, RuntimeError, ValueError):
                    paths.append(str(Path(storage_path).expanduser().resolve()))
                continue

            service = service or _configured_file_service()
            if service is None:
                logger.warning(
                    "chat_context_s3_materialization_skipped",
                    file_id=file_id,
                    reason="file_service_unavailable",
                )
                continue
            staged = await _stage_s3_authorized_file_row(
                row,
                file_service=service,
                staged_paths=staged_paths,
            )
            if staged is not None:
                paths.append(staged)
        yield dedupe_resolved_paths(paths)
    finally:
        for staged_path in reversed(staged_paths):
            with suppress(OSError):
                staged_path.unlink()


@asynccontextmanager
async def materialize_folder_context_paths(
    user_id: UUID,
    db: DatabaseService,
    *,
    folder_id: str | None = None,
    file_ids_csv: str | None = None,
    extra_folder_id: str | None = None,
    file_service: Any | None = None,
) -> AsyncIterator[list[str]]:
    """Stage S3 files selected by folder or ``file_ids`` for a native chat turn.

    Every row is owner-filtered before entering the generic materialization
    helper.  ``extra_folder_id`` mirrors the native chat project's second
    folder lookup without allowing a caller to turn a raw S3 URI into a ref.
    """
    rows = await _resolve_authorized_folder_context_rows(
        user_id,
        db,
        folder_id=folder_id,
        file_ids_csv=file_ids_csv,
    )
    if extra_folder_id:
        extra_rows = await _resolve_authorized_folder_context_rows(
            user_id,
            db,
            folder_id=extra_folder_id,
        )
        seen_storage_paths = {str(getattr(row, "storage_path", "") or "") for row in rows}
        for row in extra_rows:
            storage_path = str(getattr(row, "storage_path", "") or "")
            if storage_path not in seen_storage_paths:
                rows.append(row)
                seen_storage_paths.add(storage_path)

    async with _materialize_authorized_file_rows(
        rows,
        file_service=file_service,
    ) as paths:
        yield paths


@asynccontextmanager
async def materialize_knowledge_message_paths(
    user_id: UUID,
    db: DatabaseService | None,
    message: str,
    *,
    file_service: Any | None = None,
) -> AsyncIterator[list[str]]:
    """Yield authorised local paths, staging S3 data until this turn ends.

    The ACL query runs before ``FileService.download``.  The ``finally`` block
    removes every temporary file after agent completion, generator shutdown,
    disconnect, or exception.
    """
    rows = [] if db is None else await _resolve_authorized_knowledge_rows(user_id, db, message)
    async with _materialize_authorized_file_rows(
        rows,
        file_service=file_service,
    ) as paths:
        yield paths
