"""File operations API endpoints."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import mimetypes
import os
import re
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy import update as sa_update
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession
from sse_starlette.sse import EventSourceResponse
from starlette.background import BackgroundTask

from StAgent.api.v1.folder_items import _decrement_owned_folder_count
from StAgent.config.constants import MAX_UPLOAD_SIZE_BYTES
from StAgent.config.settings import get_settings
from StAgent.db import DatabaseService, get_database_service
from StAgent.db.models import (
    File as FileModel,
)
from StAgent.db.models import (
    FileRead,
    FileStatus,
    FileType,
    InboxState,
    LibraryScope,
)
from StAgent.db.models.folder import Folder
from StAgent.db.sqlite_compat import (
    file_model_from_sqlite_row,
    load_entity_by_id,
    same_user_id,
    sqlite_parent_id_text,
    uuid_column_equals,
)
from StAgent.db.sqlite_compat import session_dialect_name as _session_dialect_name
from StAgent.file.primitives import classify_file_kind, sanitize_filename
from StAgent.knowledge.acl import knowledge_file_access_clause
from StAgent.services.auth import (
    CurrentUserId,
    OptionalUserId,
    SignedUrlError,
    verify_signed_token,
)
from StAgent.tasks.jobs import enqueue_file_processing

_logger = logging.getLogger(__name__)

router = APIRouter()


def _upload_dir() -> str:
    """Return the canonical on-disk upload directory."""
    return get_settings().files.upload_dir


MAX_FILE_SIZE = MAX_UPLOAD_SIZE_BYTES


async def _store_blob(
    content: bytes,
    *,
    filename: str,
    content_type: str | None,
    user_id: UUID | None,
    session_id: UUID | None,
    library_scope: str = "workspace",
    origin_type: str | None = "upload",
    origin_ref: str | None = None,
    category: str = "upload",
):
    """Persist *content* through the single managed-blob ingress.

    Returns the resulting :class:`~StAgent.file.service.FileRef`. The DB row is
    written separately by the caller (``persist_db_row=False``) because the
    ``files`` endpoint owns a richer row (folder/workspace linkage, processing
    state).
    """
    from StAgent.file.primitives import FileScope
    from StAgent.services.service_manager import get_service_manager

    sm = get_service_manager()
    fs = sm.file_service
    if fs is None:
        from StAgent.file.service import FileService
        from StAgent.file.storage.local import LocalStorageBackend

        fs = FileService(
            default_backend=LocalStorageBackend(_upload_dir()),
            default_backend_name="local",
            database=sm.db,
        )
    return await fs.register(
        content,
        filename=filename,
        content_type=content_type,
        user_id=user_id,
        session_id=session_id,
        scope=FileScope.SESSION,
        category=category,
        library_scope=library_scope,
        origin_type=origin_type,
        origin_ref=origin_ref,
        persist_db_row=False,
    )


def _managed_file_service(db: DatabaseService) -> Any:
    """Return the configured managed storage service for an endpoint DB."""
    from StAgent.services.service_manager import get_service_manager

    sm = get_service_manager()
    if sm.file_service is not None and getattr(sm, "db", None) is db:
        return sm.file_service

    from StAgent.file.service import FileService
    from StAgent.file.storage.local import LocalStorageBackend

    return FileService(
        default_backend=LocalStorageBackend(_upload_dir()),
        default_backend_name="local",
        database=db,
    )


async def _read_managed_file_bytes(
    file: FileModel,
    db: DatabaseService,
) -> tuple[bytes, str]:
    """Read a catalog file through its configured storage backend.

    Legacy local rows retain a direct-file fallback. S3 rows are always read
    through ``FileService`` so a restart does not reinterpret an ``s3://`` URI
    as a local path.
    """
    storage_path = str(file.storage_path or "")
    if storage_path and os.path.isfile(storage_path):
        raw = await asyncio.to_thread(Path(storage_path).read_bytes)
        return raw, file.mime_type or "application/octet-stream"

    service = _managed_file_service(db)
    ref = service.ref_from_persisted_record(
        file_id=file.id,
        filename=file.name,
        storage_path=storage_path,
        content_type=file.mime_type,
        size=file.size,
        checksum=file.checksum,
        user_id=file.user_id,
        session_id=file.session_id,
        library_scope=str(getattr(file.library_scope, "value", file.library_scope) or "workspace"),
    )
    try:
        return await service.download(ref)
    except FileNotFoundError:
        pass
    raise FileNotFoundError(f"Managed file is unavailable: {file.id}")


async def _managed_file_exists(file: FileModel, db: DatabaseService) -> bool:
    """Check either a legacy local path or the configured object store."""
    storage_path = str(file.storage_path or "")
    if storage_path and os.path.isfile(storage_path):
        return True
    try:
        service = _managed_file_service(db)
        ref = service.ref_from_persisted_record(
            file_id=file.id,
            filename=file.name,
            storage_path=storage_path,
            content_type=file.mime_type,
            size=file.size,
            checksum=file.checksum,
            user_id=file.user_id,
            session_id=file.session_id,
            library_scope=str(getattr(file.library_scope, "value", file.library_scope) or "workspace"),
        )
        return await service.exists(ref)
    except (FileNotFoundError, RuntimeError):
        return False


def _bytes_file_response(
    raw: bytes,
    *,
    filename: str | None,
    media_type: str,
    disposition: str,
    headers: dict[str, str] | None = None,
) -> Response:
    """Create a safe response for object-store bytes without exposing a path."""
    safe_name = sanitize_filename(filename or "download") or "download"
    ascii_name = safe_name.encode("ascii", "ignore").decode() or "download"
    response_headers = dict(headers or {})
    response_headers["Content-Disposition"] = (
        f"{disposition}; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(safe_name)}"
    )
    return Response(content=raw, media_type=media_type, headers=response_headers)


# Multi-file ZIP download (POST /files/bundle/download)
MAX_BUNDLE_FILE_COUNT = 80
MAX_BUNDLE_TOTAL_BYTES = 400 * 1024 * 1024  # 400 MB uncompressed sum (guardrail)


def resolve_pet_space_upload_mime_type(
    filename: str | None,
    declared: str | None,
    content: bytes,
) -> str | None:
    """Infer a useful MIME for Pet Space uploads when the client sends a weak ``Content-Type``.

    Browsers sometimes upload SVG/GIF/PNG as ``application/octet-stream``; Pet Space uses MIME to
    decide whether a file can be shown as a pet appearance in the dock.
    """
    d = (declared or "").strip() or None
    if d and d != "application/octet-stream":
        return d
    if filename:
        guessed, _ = mimetypes.guess_type(filename)
        if guessed:
            # Windows' registry-backed mimetypes maps ``.svg`` to the non-canonical
            # ``image/svg`` (no ``+xml``); normalise so downstream checks match POSIX.
            if guessed == "image/svg":
                return "image/svg+xml"
            return guessed
    if len(content) >= 6 and content[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp"
    head = content[:8192].lstrip()
    if head.startswith(b"<?xml") or head.startswith(b"<svg") or b"<svg" in content[:8192]:
        return "image/svg+xml"
    return d


class FileBundleDownloadRequest(BaseModel):
    """Request body for zipping multiple owned files into one archive."""

    file_ids: list[UUID] = Field(
        ...,
        min_length=1,
        max_length=MAX_BUNDLE_FILE_COUNT,
        description="File UUIDs to include (order preserved; duplicates removed).",
    )
    filename: str | None = Field(
        default=None,
        max_length=200,
        description="Suggested download name; must end in .zip after sanitization.",
    )


def _sanitize_bundle_download_filename(raw: str | None) -> str:
    """Return a safe single-segment ``*.zip`` filename for Content-Disposition."""
    base = (raw or "workspace-files.zip").strip() or "workspace-files.zip"
    base = os.path.basename(base.replace("\\", "/"))
    base = re.sub(r"[^A-Za-z0-9._\-\u4e00-\u9fff]+", "_", base).strip("._") or "workspace-files"
    if not base.lower().endswith(".zip"):
        base = f"{base}.zip"
    return base[:180]


def _sanitize_bundle_arcname(raw: str | None, file_id: UUID) -> str:
    """Return a safe single-segment member name for the generated ZIP."""
    fallback = f"file-{file_id}"
    base = os.path.basename((raw or "").replace("\\", "/")).strip()
    safe = sanitize_filename(base, default=fallback, max_length=180).strip("._")
    return safe or fallback


def _unlink_quiet(path: str) -> None:
    with contextlib.suppress(OSError):
        os.unlink(path)


async def _load_owned_folder_for_file_write(
    session: AsyncSession,
    folder_id: UUID,
    user_id: UUID,
) -> Folder:
    """Load a writable folder and verify it belongs to ``user_id``."""
    folder = await load_entity_by_id(session, Folder, folder_id, parent_table="folders")
    if folder is None or folder.is_deleted or not same_user_id(folder.user_id, user_id):
        raise ValueError("Folder not found or access denied")
    return folder


class FileUploadResponse(BaseModel):
    """Response for file upload."""

    id: UUID
    name: str
    original_name: str
    file_type: FileType
    mime_type: str | None
    size: int
    checksum: str


class FileRenameRequest(BaseModel):
    """Rename a catalog file."""

    name: str = Field(..., min_length=1, max_length=255)


class FileSearchResult(BaseModel):
    """A single file or chunk-level search hit."""

    file_id: UUID
    file_name: str
    file_type: FileType
    score: float
    snippet: str | None = None
    chunk_id: UUID | None = None
    start_offset: int | None = None
    end_offset: int | None = None


class FileSearchResponse(BaseModel):
    query: str
    results: list[FileSearchResult]
    total: int


_FILE_KIND_TO_TYPE = {
    "image": FileType.IMAGE,
    "document": FileType.DOCUMENT,
    "data": FileType.DATA,
    "audio": FileType.AUDIO,
    "video": FileType.VIDEO,
    "archive": FileType.ARCHIVE,
    "code": FileType.CODE,
    "text": FileType.DOCUMENT,
}


def get_file_type(mime_type: str | None, filename: str | None = None) -> FileType:
    """Determine :class:`FileType` via the canonical :func:`classify_file_kind`."""
    kind = classify_file_kind(filename or "", mime_type)
    return _FILE_KIND_TO_TYPE.get(kind.value, FileType.OTHER)


async def _background_process_file(
    file_id: str, storage_path: str, mime_type: str | None, original_name: str | None
) -> None:
    """Background task to auto-process an uploaded file."""
    try:
        from StAgent.services.service_manager import get_service_manager

        sm = get_service_manager()
        if sm.file_processing:
            await sm.file_processing.process_and_update_db(
                file_id=file_id,
                file_path=storage_path,
                mime_type=mime_type,
                original_name=original_name,
            )
    except Exception as exc:
        _logger.warning("Background file processing failed for %s: %s", file_id, exc)


async def persist_uploaded_file(
    upload: UploadFile,
    user_id: UUID,
    db: DatabaseService,
    *,
    session_id: UUID | None = None,
    folder_id: UUID | None = None,
    library_scope: LibraryScope = LibraryScope.WORKSPACE,
    origin_type: str | None = "upload",
) -> FileModel:
    """Save an UploadFile to disk + DB and schedule text extraction.

    ``session_id`` is optional for folder/library uploads that are not tied to
    a chat turn. Chat attachments still pass a non-null ``session_id``.
    """
    if not upload.filename:
        raise ValueError("Filename is required")

    content = await upload.read()
    file_size = len(content)

    if file_size > MAX_FILE_SIZE:
        raise ValueError(f"File too large ({file_size} bytes). Maximum is {MAX_FILE_SIZE // (1024 * 1024)}MB")

    if folder_id is not None:
        async with db.session() as session:
            await _load_owned_folder_for_file_write(session, folder_id, user_id)

    ref = await _store_blob(
        content,
        filename=upload.filename,
        content_type=upload.content_type,
        user_id=user_id,
        session_id=session_id,
        library_scope=library_scope.value,
        origin_type=origin_type,
    )
    file_id = ref.id
    safe_name = os.path.basename(ref.storage_key)
    storage_path = ref.metadata.get("storage_path", ref.storage_key)
    checksum = ref.checksum

    file_type = get_file_type(upload.content_type, upload.filename)

    async with db.session() as session:
        folder: Folder | None = None
        if folder_id is not None:
            folder = await _load_owned_folder_for_file_write(session, folder_id, user_id)

        db_file = FileModel(
            id=file_id,
            session_id=session_id,
            name=safe_name,
            original_name=upload.filename,
            file_type=file_type,
            mime_type=upload.content_type,
            size=file_size,
            status=FileStatus.UPLOADED,
            user_id=user_id,
            folder_id=folder_id,
            storage_path=storage_path,
            checksum=checksum,
            library_scope=library_scope,
            inbox_state=InboxState.NEW,
            origin_type=origin_type,
        )
        session.add(db_file)
        await session.flush()
        await session.refresh(db_file)

        if folder is not None:
            folder.file_count = int(folder.file_count or 0) + 1
            session.add(folder)
            await session.flush()

    await enqueue_file_processing(
        db,
        file_id=str(file_id),
        storage_path=storage_path,
        mime_type=upload.content_type,
        original_name=upload.filename,
        user_id=user_id,
        session_id=session_id,
    )

    return db_file


async def persist_pet_space_file(
    upload: UploadFile,
    user_id: UUID,
    db: DatabaseService,
    *,
    workspace_id: UUID | None,
) -> FileModel:
    """Save an upload to disk + DB for Pet Space (no chat session).

    The row has ``session_id is NULL`` and is linked via :class:`PetProjectFile`.
    """
    if not upload.filename:
        raise ValueError("Filename is required")

    content = await upload.read()
    file_size = len(content)

    if file_size > MAX_FILE_SIZE:
        raise ValueError(f"File too large ({file_size} bytes). Maximum is {MAX_FILE_SIZE // (1024 * 1024)}MB")

    resolved_mime = resolve_pet_space_upload_mime_type(upload.filename, upload.content_type, content)

    ref = await _store_blob(
        content,
        filename=upload.filename,
        content_type=resolved_mime,
        user_id=user_id,
        session_id=None,
    )
    file_id = ref.id
    safe_name = os.path.basename(ref.storage_key)
    storage_path = ref.metadata.get("storage_path", ref.storage_key)
    checksum = ref.checksum

    file_type = get_file_type(resolved_mime, upload.filename)

    async with db.session() as session:
        if _session_dialect_name(session) == "sqlite":
            u_txt = await sqlite_parent_id_text(session, "users", user_id)
            w_txt: str | None
            if workspace_id is not None:
                w_txt = await sqlite_parent_id_text(session, "workspaces", workspace_id)
            else:
                w_txt = None
            now = datetime.utcnow()
            # Store Alembic / SQLAlchemy enum *names* (IMAGE, UPLOADED), not str-Enum values (image).
            ft_val = file_type.name if isinstance(file_type, FileType) else str(file_type)
            await session.execute(
                text(
                    """
                    INSERT INTO files (
                        is_deleted, deleted_at, created_at, updated_at, id,
                        name, original_name, file_type, mime_type, size, status,
                        user_id, workspace_id, folder_id, session_id,
                        storage_path, storage_bucket, checksum,
                        extracted_text, file_metadata, page_count, has_ocr, ocr_language,
                        embedding_id, is_indexed, expires_at
                    ) VALUES (
                        0, NULL, :c1, :c2, :id,
                        :name, :oname, :ftype, :mime, :size, :status,
                        :uid, """
                    + ("NULL" if w_txt is None else ":wid")
                    + """, NULL, NULL,
                        :path, NULL, :checksum,
                        NULL, NULL, NULL, 0, NULL,
                        NULL, 0, NULL
                    )
                    """
                ),
                (
                    {
                        "c1": now,
                        "c2": now,
                        "id": file_id.hex,
                        "name": safe_name,
                        "oname": upload.filename,
                        "ftype": ft_val,
                        "mime": resolved_mime,
                        "size": file_size,
                        "status": FileStatus.UPLOADED.name,
                        "uid": u_txt,
                        "path": storage_path,
                        "checksum": checksum,
                    }
                    | ({"wid": w_txt} if w_txt is not None else {})
                ),
            )
            await session.flush()
            db_file = FileModel(
                id=file_id,
                session_id=None,
                name=safe_name,
                original_name=upload.filename,
                file_type=file_type,
                mime_type=resolved_mime,
                size=file_size,
                status=FileStatus.UPLOADED,
                user_id=user_id,
                workspace_id=workspace_id,
                folder_id=None,
                storage_path=storage_path,
                checksum=checksum,
                is_deleted=False,
                deleted_at=None,
                created_at=now,
                updated_at=now,
            )
        else:
            db_file = FileModel(
                id=file_id,
                session_id=None,
                name=safe_name,
                original_name=upload.filename,
                file_type=file_type,
                mime_type=resolved_mime,
                size=file_size,
                status=FileStatus.UPLOADED,
                user_id=user_id,
                workspace_id=workspace_id,
                folder_id=None,
                storage_path=storage_path,
                checksum=checksum,
            )
            session.add(db_file)
            await session.flush()
            await session.refresh(db_file)

    await enqueue_file_processing(
        db,
        file_id=str(file_id),
        storage_path=storage_path,
        mime_type=resolved_mime,
        original_name=upload.filename,
        user_id=user_id,
        session_id=None,
    )

    return db_file


@router.post("/upload", response_model=FileUploadResponse)
async def upload_file(
    user_id: CurrentUserId,
    db: Annotated[DatabaseService, Depends(get_database_service)],
    file: UploadFile = File(...),
    session_id: UUID | None = Form(default=None),
    folder_id: UUID | None = Form(default=None),
    library_scope: str | None = Form(default=None),
) -> FileUploadResponse:
    """Upload a file to the unified catalog.

    ``session_id`` is optional — folder/library uploads may omit it. Chat
    attachments should still pass the owning session id.
    """
    if not file.filename:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Filename is required",
        )

    if file.size and file.size > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File too large. Maximum size is {MAX_FILE_SIZE // (1024 * 1024)}MB",
        )

    scope = LibraryScope.WORKSPACE
    if library_scope:
        try:
            scope = LibraryScope(library_scope)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid library_scope: {library_scope}",
            )

    try:
        effective_folder_id = folder_id
        if effective_folder_id is None and session_id is not None:
            with contextlib.suppress(Exception):
                from StAgent.services.chat.project_files import resolve_session_project_file_space

                space = await resolve_session_project_file_space(db, session_id=session_id, user_id=user_id)
                if space is not None:
                    effective_folder_id = space.folder_id

        db_file = await persist_uploaded_file(
            file,
            user_id,
            db,
            session_id=session_id,
            folder_id=effective_folder_id,
            library_scope=scope,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    return FileUploadResponse(
        id=db_file.id,
        name=db_file.name,
        original_name=db_file.original_name,
        file_type=db_file.file_type,
        mime_type=db_file.mime_type,
        size=db_file.size,
        checksum=db_file.checksum or "",
    )


@router.get("/search", response_model=FileSearchResponse)
async def search_files(
    user_id: CurrentUserId,
    db: Annotated[DatabaseService, Depends(get_database_service)],
    query: str = Query(..., min_length=1, max_length=1000),
    library_scope: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
) -> FileSearchResponse:
    """Search the user's catalog by filename and indexed document chunks."""
    from sqlmodel import col, or_, select

    scope_filter: LibraryScope | None = None
    if library_scope:
        try:
            scope_filter = LibraryScope(library_scope)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid library_scope: {library_scope}",
            )

    hits: list[FileSearchResult] = []
    seen_files: set[UUID] = set()

    chunk_hits = await db.repositories.document_chunks.search(
        user_id,
        query,
        limit=limit,
        library_scope=scope_filter.value if scope_filter else None,
    )
    # Batch-load the referenced files in one IN query instead of one
    # session + lookup per chunk hit.
    if chunk_hits:
        hit_file_ids = list(dict.fromkeys(h.file_id for h in chunk_hits))
        files_by_id = {
            f.id: f for f in await db.repositories.files.get_many_for_user(hit_file_ids, user_id)
        }
    else:
        files_by_id = {}
    for hit in chunk_hits:
        f = files_by_id.get(hit.file_id)
        if f is None:
            continue
        if scope_filter is not None and f.library_scope != scope_filter:
            continue
        seen_files.add(hit.file_id)
        hits.append(
            FileSearchResult(
                file_id=hit.file_id,
                file_name=hit.file_name,
                file_type=f.file_type,
                score=hit.score,
                snippet=hit.snippet,
                chunk_id=hit.chunk_id,
                start_offset=hit.start_offset,
                end_offset=hit.end_offset,
            )
        )

    remaining = max(0, limit - len(hits))
    if remaining > 0:
        async with db.session() as session:
            name_q = select(FileModel).where(
                uuid_column_equals(session, FileModel.user_id, user_id),
                FileModel.is_deleted == False,  # noqa: E712
                or_(
                    FileModel.name.ilike(f"%{query}%"),  # type: ignore[attr-defined]
                    FileModel.original_name.ilike(f"%{query}%"),  # type: ignore[attr-defined]
                ),
            )
            if scope_filter is not None:
                name_q = name_q.where(FileModel.library_scope == scope_filter)
            name_q = name_q.order_by(col(FileModel.created_at).desc()).limit(remaining)
            result = await session.exec(name_q)
            for f in result.all():
                if f.id in seen_files:
                    continue
                hits.append(
                    FileSearchResult(
                        file_id=f.id,
                        file_name=f.original_name,
                        file_type=f.file_type,
                        score=0.5,
                        snippet=None,
                    )
                )

    return FileSearchResponse(query=query, results=hits, total=len(hits))


@router.patch("/{file_id}", response_model=FileRead)
async def rename_file(
    file_id: UUID,
    body: FileRenameRequest,
    user_id: CurrentUserId,
    db: Annotated[DatabaseService, Depends(get_database_service)],
) -> FileRead:
    """Rename a catalog file (display name only; storage key unchanged)."""
    safe = sanitize_filename(body.name)
    async with db.session() as session:
        file = await load_entity_by_id(session, FileModel, file_id, parent_table="files")
        if file is None or file.is_deleted:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")
        if not same_user_id(file.user_id, user_id):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")
        await session.execute(
            sa_update(FileModel)
            .where(uuid_column_equals(session, FileModel.id, file_id))
            .values(name=safe, original_name=safe, updated_at=datetime.utcnow())
        )
        updated = await load_entity_by_id(session, FileModel, file_id, parent_table="files")
        return FileRead.model_validate(updated or file)


@router.post("/bundle/download")
async def download_file_bundle(
    body: FileBundleDownloadRequest,
    user_id: CurrentUserId,
    db: Annotated[DatabaseService, Depends(get_database_service)],
) -> FileResponse:
    """Zip multiple files the user owns into one ``.zip`` (JWT only; not signed-URL).

    Used by the workspace Files panel for compressed bulk download. For in-session
    automation the agent should use ``archive_manager`` (create/list/extract).
    """
    unique_ids = list(dict.fromkeys(body.file_ids))
    out_name = _sanitize_bundle_download_filename(body.filename)

    rows = await db.repositories.files.get_many_for_user(unique_ids, user_id)

    found = {f.id for f in rows}
    missing = [str(i) for i in unique_ids if i not in found]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"One or more files were not found or not owned by you: {', '.join(missing[:8])}",
        )

    id_to_row = {f.id: f for f in rows}
    ordered = [id_to_row[i] for i in unique_ids]

    total_size = sum(max(0, f.size) for f in ordered)
    if total_size > MAX_BUNDLE_TOTAL_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=(
                f"Combined file size ({total_size} bytes) exceeds bundle limit "
                f"({MAX_BUNDLE_TOTAL_BYTES} bytes). Download fewer files or use the agent "
                "archive_manager tool for server-side archives."
            ),
        )

    with tempfile.NamedTemporaryFile(delete=False, suffix=".zip") as tmp_f:
        tmp_path = tmp_f.name

    used_arcnames: set[str] = set()
    try:
        with zipfile.ZipFile(
            tmp_path,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=6,
        ) as zf:
            for f in ordered:
                arc = _sanitize_bundle_arcname(f.original_name or f.name, f.id)
                base, ext = os.path.splitext(arc)
                candidate = arc
                n = 1
                while candidate in used_arcnames:
                    candidate = f"{base}_{n}{ext}"
                    n += 1
                used_arcnames.add(candidate)
                if str(f.storage_path or "").startswith("s3://"):
                    try:
                        raw, _content_type = await _read_managed_file_bytes(f, db)
                    except (FileNotFoundError, RuntimeError) as exc:
                        raise HTTPException(
                            status_code=status.HTTP_404_NOT_FOUND,
                            detail=f"File missing on storage: {f.id}",
                        ) from exc
                    zf.writestr(candidate, raw)
                elif f.storage_path and os.path.isfile(f.storage_path):
                    zf.write(f.storage_path, arcname=candidate)
                else:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail=f"File missing on storage: {f.id}",
                    )
    except HTTPException:
        _unlink_quiet(tmp_path)
        raise
    except OSError as exc:
        _unlink_quiet(tmp_path)
        _logger.warning("bundle_zip_failed", error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not build zip archive",
        ) from exc

    return FileResponse(
        tmp_path,
        media_type="application/zip",
        filename=out_name,
        background=BackgroundTask(_unlink_quiet, tmp_path),
    )


async def _resolve_file_for_serve(
    file_id: UUID,
    *,
    token: str | None,
    jwt_user_id: UUID | None,
    required_scope: str,
    db: DatabaseService,
) -> FileModel:
    """Authorise + load a :class:`FileModel` row for download/preview.

    Two authentication paths are accepted:

    * **Signed URL** — ``?token=<signed>`` query param whose HMAC scope
      matches ``required_scope`` and whose embedded ``uid`` matches the
      file's owner. This lets ``<img src>`` / ``<object data>`` tags
      render attachments without a second round-trip for the bearer.
    * **JWT bearer** — the standard ``Authorization: Bearer`` header for
      direct API consumers.

    Returns the :class:`FileModel` row so the caller can build the
    :class:`FileResponse`. Raises :class:`HTTPException` on any auth
    failure so the endpoint shape stays flat.
    """
    settings = get_settings()
    owner_id_from_token: UUID | None = None
    if token:
        try:
            signed = verify_signed_token(settings, token)
        except SignedUrlError as exc:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
        if signed.scope != required_scope:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Signed URL scope mismatch (want {required_scope}, got {signed.scope})",
            )
        if signed.attachment_id != file_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Signed URL does not match file id",
            )
        owner_id_from_token = signed.user_id
    elif jwt_user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="File access requires either a signed URL token or a Bearer JWT",
        )

    async with db.session() as session:
        if _session_dialect_name(session) == "sqlite":
            id_txt = await sqlite_parent_id_text(session, "files", file_id)
            r = await session.execute(
                text("SELECT * FROM files WHERE CAST(id AS TEXT) = :id"),
                {"id": id_txt},
            )
            row = r.mappings().first()
            if row is None or row["is_deleted"]:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")
            file = file_model_from_sqlite_row(row)
        else:
            file = await session.get(FileModel, file_id)
            if not file or file.is_deleted:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")

        effective_user = owner_id_from_token or jwt_user_id
        is_owner = effective_user is None or same_user_id(file.user_id, effective_user)
        has_knowledge_access = False
        if (
            not is_owner
            and owner_id_from_token is None
            and jwt_user_id is not None
            and file.library_scope == LibraryScope.KNOWLEDGE
        ):
            has_knowledge_access = (
                await session.exec(
                    select(FileModel.id)
                    .where(uuid_column_equals(session, FileModel.id, file_id))
                    .where(FileModel.is_deleted == False)  # noqa: E712
                    .where(FileModel.library_scope == LibraryScope.KNOWLEDGE)
                    .where(knowledge_file_access_clause(session, jwt_user_id))
                )
            ).first() is not None
        if not is_owner and not has_knowledge_access:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied to this file",
            )

        if not str(file.storage_path or "").startswith("s3://") and not os.path.exists(file.storage_path):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="File not found on storage",
            )
        return file


_INLINE_IMAGE_MIME_TYPES = frozenset(
    {
        "image/avif",
        "image/bmp",
        "image/gif",
        "image/heic",
        "image/jpeg",
        "image/png",
        "image/tiff",
        "image/webp",
        "image/x-icon",
    }
)
_INLINE_EXACT_MIME_TYPES = frozenset({"application/pdf"})
_INLINE_MIME_PREFIXES = ("audio/", "video/")


def preview_response_policy(mime: str | None) -> tuple[str, str]:
    """Return the MIME and disposition allowed for a browser file preview.

    Uploaded HTML, SVG, XML, and JavaScript are active content. They must be
    downloaded rather than rendered in a same-origin tab or converted into a
    blob URL by an authenticated client.
    """
    normalized = (mime or "").split(";", 1)[0].strip().lower()
    if (
        normalized in _INLINE_IMAGE_MIME_TYPES
        or normalized in _INLINE_EXACT_MIME_TYPES
        or any(normalized.startswith(prefix) for prefix in _INLINE_MIME_PREFIXES)
    ):
        return normalized, "inline"
    return "application/octet-stream", "attachment"


@router.get("/events/stream")
async def stream_file_events(
    user_id: CurrentUserId,
    request: Request,
    StAgent_machine_fp: str | None = Query(default=None),
) -> EventSourceResponse:
    """Keep the SPA file-sync EventSource connected.

    File mutations are already reflected through ordinary HTTP responses and
    local browser events. This endpoint provides the server-side SSE contract
    the SPA expects, avoiding repeated 404 reconnect noise until cross-worker
    file event fan-out is introduced.
    """
    del user_id, StAgent_machine_fp

    async def _gen():
        try:
            yield {"event": "ready", "data": "{}"}
            while not await request.is_disconnected():
                await asyncio.sleep(15)
                yield {"event": "heartbeat", "data": "{}"}
        except asyncio.CancelledError:
            return

    return EventSourceResponse(_gen(), media_type="text/event-stream")


@router.get("/{file_id}")
@router.get("/{file_id}/download")
async def download_file(
    file_id: UUID,
    db: Annotated[DatabaseService, Depends(get_database_service)],
    user_id: OptionalUserId = None,
    token: str | None = Query(default=None),
) -> Response:
    """Download a file by ID.

    Accepts either a bearer JWT or a ``?token=`` signed URL with
    ``scope=download`` (see :mod:`StAgent.services.auth.signed_url`).
    """
    file = await _resolve_file_for_serve(
        file_id,
        token=token,
        jwt_user_id=user_id,
        required_scope="download",
        db=db,
    )
    if str(file.storage_path or "").startswith("s3://"):
        try:
            raw, content_type = await _read_managed_file_bytes(file, db)
        except (FileNotFoundError, RuntimeError) as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="File not found on storage",
            ) from exc
        return _bytes_file_response(
            raw,
            filename=file.original_name,
            media_type=file.mime_type or content_type,
            disposition="attachment",
        )
    return FileResponse(
        path=file.storage_path,
        filename=file.original_name,
        media_type=file.mime_type or "application/octet-stream",
    )


@router.get("/{file_id}/preview")
async def preview_file(
    file_id: UUID,
    db: Annotated[DatabaseService, Depends(get_database_service)],
    user_id: OptionalUserId = None,
    token: str | None = Query(default=None),
) -> Response:
    """Serve a session attachment inline for in-browser preview.

    Accepts either a bearer JWT or a ``?token=`` signed URL with
    ``scope=preview``. The response uses ``Content-Disposition: inline``
    and — for non-renderable MIME types — falls back to
    ``application/octet-stream`` so the browser prompts a download
    instead of leaking content to an inline viewer that can't handle
    it safely.
    """
    file = await _resolve_file_for_serve(
        file_id,
        token=token,
        jwt_user_id=user_id,
        required_scope="preview",
        db=db,
    )
    mime, disposition = preview_response_policy(file.mime_type)
    headers = {
        "Cache-Control": "private, max-age=60",
        "X-Content-Type-Options": "nosniff",
    }
    if str(file.storage_path or "").startswith("s3://"):
        try:
            raw, content_type = await _read_managed_file_bytes(file, db)
        except (FileNotFoundError, RuntimeError) as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="File not found on storage",
            ) from exc
        return _bytes_file_response(
            raw,
            filename=file.original_name,
            media_type=mime or content_type,
            disposition=disposition,
            headers=headers,
        )
    return FileResponse(
        path=file.storage_path,
        filename=file.original_name,
        media_type=mime,
        content_disposition_type=disposition,
        headers=headers,
    )


@router.delete("/{file_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_file(
    file_id: UUID,
    user_id: CurrentUserId,
    db: Annotated[DatabaseService, Depends(get_database_service)],
) -> None:
    """Delete a file (soft delete)."""
    async with db.session() as session:
        if _session_dialect_name(session) == "sqlite":
            id_txt = await sqlite_parent_id_text(session, "files", file_id)
            r = await session.execute(
                text("SELECT user_id, is_deleted, folder_id FROM files WHERE CAST(id AS TEXT) = :id"),
                {"id": id_txt},
            )
            row = r.mappings().first()
            if row is None or row["is_deleted"]:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="File not found",
                )
            if not same_user_id(row["user_id"], user_id):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Access denied to this file",
                )
            now = datetime.utcnow()
            await session.execute(
                text("UPDATE files SET is_deleted = 1, deleted_at = :d, updated_at = :u WHERE CAST(id AS TEXT) = :id"),
                {"d": now, "u": now, "id": id_txt},
            )
            folder_id = UUID(str(row["folder_id"])) if row["folder_id"] else None
            await _decrement_owned_folder_count(session, folder_id, user_id)
            return

        file = await session.get(FileModel, file_id)

        if not file or file.is_deleted:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="File not found",
            )

        if not same_user_id(file.user_id, user_id):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied to this file",
            )

        await _decrement_owned_folder_count(session, file.folder_id, user_id)
        file.is_deleted = True
        file.deleted_at = datetime.utcnow()
        file.updated_at = datetime.utcnow()
        session.add(file)


@router.get("/{file_id}/info", response_model=FileRead)
async def get_file_info(
    file_id: UUID,
    user_id: CurrentUserId,
    db: Annotated[DatabaseService, Depends(get_database_service)],
) -> FileRead:
    """Get file metadata without downloading."""
    async with db.session() as session:
        if _session_dialect_name(session) == "sqlite":
            id_txt = await sqlite_parent_id_text(session, "files", file_id)
            r = await session.execute(
                text("SELECT * FROM files WHERE CAST(id AS TEXT) = :id"),
                {"id": id_txt},
            )
            row = r.mappings().first()
            if row is None or row["is_deleted"]:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="File not found",
                )
            file = file_model_from_sqlite_row(row)
            if not same_user_id(file.user_id, user_id):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Access denied to this file",
                )
            return FileRead.model_validate(file)

        file = await session.get(FileModel, file_id)

        if not file or file.is_deleted:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="File not found",
            )

        if not same_user_id(file.user_id, user_id):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied to this file",
            )

        return FileRead.model_validate(file)
