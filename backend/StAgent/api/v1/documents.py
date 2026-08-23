"""Document management API endpoints."""

from __future__ import annotations

import os
from datetime import datetime
from typing import Annotated, Any
from uuid import UUID  # noqa: TC003 - FastAPI resolves endpoint annotations at runtime

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy import or_
from sqlalchemy import update as sa_update
from sqlmodel import col, func, select

from StAgent.config.settings import get_settings
from StAgent.db import DatabaseService, get_database_service
from StAgent.db.models import (
    ChatSession,
    FileRead,
    FileStatus,
    FileType,
    InboxState,
    KnowledgeAccessScope,
    KnowledgeSpace,
    KnowledgeSpaceMember,
    KnowledgeSpaceMemberRole,
    LibraryScope,
    PetProject,
    PetProjectFile,
)
from StAgent.db.models import (
    File as FileModel,
)
from StAgent.db.sqlite_compat import load_entity_by_id, same_user_id, uuid_column_equals, uuid_column_in
from StAgent.file.primitives import classify_file_kind
from StAgent.knowledge.acl import knowledge_file_access_clause, knowledge_file_write_clause
from StAgent.schema.api import PaginatedResponse
from StAgent.services.auth import CurrentUserId  # noqa: TC001 - FastAPI dependency alias

router = APIRouter()


def _pet_project_library_file_ids_subquery(session: Any, user_id: UUID):
    """File IDs linked to Pet Space projects — excluded from knowledge document lists."""
    return (
        select(PetProjectFile.file_id)
        .join(PetProject, PetProjectFile.pet_project_id == PetProject.id)
        .where(
            uuid_column_equals(session, PetProject.user_id, user_id),
            PetProject.is_deleted == False,  # noqa: E712
        )
    )


def _legacy_knowledge_documents_dir() -> str:
    """Previous default blob directory (still indexed for existing installs)."""
    return os.path.join(get_settings().files.resolved_knowledge_storage_dir(), "documents")


def _system_knowledge_blob_dir() -> str:
    """Dedicated on-disk folder for system knowledge base documents."""
    return os.path.join(get_settings().files.resolved_knowledge_storage_dir(), "system")


def _knowledge_storage_roots_norm() -> tuple[str, ...]:
    """Normalized absolute dirs that count as system knowledge storage."""
    return (
        os.path.normpath(_system_knowledge_blob_dir()),
        os.path.normpath(_legacy_knowledge_documents_dir()),
    )


def _is_system_knowledge_storage_path(storage_path: str | None) -> bool:
    """True when *storage_path* lives under the system knowledge blob dirs."""
    if not storage_path:
        return False
    nf = os.path.normpath(storage_path)
    return any(nf == root or nf.startswith(root + os.sep) for root in _knowledge_storage_roots_norm())


def _storage_path_under_system_knowledge_sql():
    """SQLModel/SQLAlchemy filter: ``storage_path`` under system knowledge roots."""
    parts: list[Any] = []
    for root in _knowledge_storage_roots_norm():
        parts.append(FileModel.storage_path == root)
        parts.append(FileModel.storage_path.startswith(root + os.sep))
    return or_(*parts)


def _upload_dir() -> str:
    """Return the canonical directory for new knowledge uploads (``…/knowledge/system``)."""
    return _system_knowledge_blob_dir()


MAX_FILE_SIZE = 100 * 1024 * 1024  # 100MB
MAX_PROMOTE_FILES = 50

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


class DocumentUploadResponse(BaseModel):
    """Response for document upload."""

    id: UUID
    name: str
    original_name: str
    file_type: FileType
    mime_type: str | None
    size: int
    status: FileStatus


class DocumentSearchRequest(BaseModel):
    """Request schema for document search."""

    query: str = Field(..., min_length=1, max_length=1000)
    file_types: list[FileType] | None = None
    folder_id: UUID | None = None
    limit: int = Field(default=10, ge=1, le=100)


class DocumentSearchResult(BaseModel):
    """Search result for a document."""

    id: UUID
    name: str
    file_type: FileType
    mime_type: str | None
    size: int
    status: FileStatus
    is_indexed: bool
    summary: str | None = None
    created_at: datetime
    folder_id: UUID | None = None
    score: float
    snippet: str | None = None
    chunk_id: UUID | None = None
    start_offset: int | None = None
    end_offset: int | None = None


class DocumentSearchResponse(BaseModel):
    """Response for document search."""

    query: str
    results: list[DocumentSearchResult]
    total: int


class DocumentReindexResponse(BaseModel):
    """Accepted document re-index job and its reset catalog state."""

    id: UUID
    status: FileStatus
    is_indexed: bool
    task_id: str | None = None


class PromoteToKnowledgeRequest(BaseModel):
    """Copy session/workspace files into the system knowledge store."""

    file_ids: list[UUID] = Field(..., min_length=1, max_length=MAX_PROMOTE_FILES)
    session_id: UUID | None = Field(
        default=None,
        description="Optional chat session id for provenance stamping.",
    )


class SkippedPromoteFile(BaseModel):
    """A source file that was not copied into the knowledge store."""

    id: UUID
    reason: str


class PromoteToKnowledgeResponse(BaseModel):
    """Result of promoting files into the system knowledge base."""

    promoted: list[DocumentUploadResponse]
    skipped: list[SkippedPromoteFile]


def get_file_type(mime_type: str | None, filename: str | None = None) -> FileType:
    """Determine :class:`FileType` via the canonical :func:`classify_file_kind`."""
    kind = classify_file_kind(filename or "", mime_type)
    return _FILE_KIND_TO_TYPE.get(kind.value, FileType.OTHER)


@router.post("/upload", response_model=DocumentUploadResponse)
async def upload_document(
    user_id: CurrentUserId,
    db: Annotated[DatabaseService, Depends(get_database_service)],
    file: UploadFile = File(...),
    session_id: UUID | None = Form(default=None),
    folder_id: UUID | None = Form(default=None),
    space_id: UUID | None = Form(default=None),
    access_scope: KnowledgeAccessScope = Form(default=KnowledgeAccessScope.PRIVATE),
    process_ocr: bool = Form(default=True),
    index_for_search: bool = Form(default=True),
) -> DocumentUploadResponse:
    """Upload a document into the knowledge catalog.

    Knowledge files are stamped with ``library_scope='knowledge'``; a dedicated
    chat session is no longer required.
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

    from StAgent.api.v1.files import _load_owned_folder_for_file_write, _store_blob

    if folder_id is not None:
        try:
            async with db.session() as session:
                await _load_owned_folder_for_file_write(session, folder_id, user_id)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(exc),
            ) from exc

    content = await file.read()
    file_size = len(content)

    from StAgent.knowledge.acl import ensure_personal_knowledge_space

    personal_space_id = await ensure_personal_knowledge_space(db, user_id)
    target_space_id = personal_space_id
    target_workspace_id = user_id
    if space_id is not None:
        async with db.session() as session:
            space = await session.get(KnowledgeSpace, space_id)
            member = (
                await session.exec(
                    select(KnowledgeSpaceMember).where(
                        uuid_column_equals(session, KnowledgeSpaceMember.space_id, space_id),
                        uuid_column_equals(session, KnowledgeSpaceMember.user_id, user_id),
                        KnowledgeSpaceMember.role.in_(
                            [KnowledgeSpaceMemberRole.MANAGER, KnowledgeSpaceMemberRole.EDITOR]
                        ),
                    )
                )
            ).first()
            if space is None or space.is_deleted or member is None:
                raise HTTPException(status_code=403, detail="Knowledge space write permission required")
            target_space_id = space.id
            target_workspace_id = space.workspace_id
    elif access_scope != KnowledgeAccessScope.PRIVATE:
        raise HTTPException(status_code=400, detail="space_id is required for shared access")

    ref = await _store_blob(
        content,
        filename=file.filename,
        content_type=file.content_type,
        user_id=user_id,
        session_id=session_id,
        library_scope=LibraryScope.KNOWLEDGE.value,
        origin_type="upload",
        category="knowledge_upload",
    )
    file_id = ref.id
    safe_name = os.path.basename(ref.storage_key)
    storage_path = ref.metadata.get("storage_path", ref.storage_key)
    checksum = ref.checksum

    file_type = get_file_type(file.content_type, file.filename)

    async with db.session() as session:
        folder = None
        if folder_id is not None:
            try:
                folder = await _load_owned_folder_for_file_write(session, folder_id, user_id)
            except ValueError as exc:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=str(exc),
                ) from exc

        db_file = FileModel(
            id=file_id,
            session_id=session_id,
            name=safe_name,
            original_name=file.filename,
            file_type=file_type,
            mime_type=file.content_type,
            size=file_size,
            status=FileStatus.UPLOADED,
            user_id=user_id,
            workspace_id=target_workspace_id,
            knowledge_space_id=target_space_id,
            access_scope=access_scope,
            folder_id=folder_id,
            storage_path=storage_path,
            checksum=checksum,
            library_scope=LibraryScope.KNOWLEDGE,
            inbox_state=InboxState.NEW,
            origin_type="upload",
            is_indexed=False,
        )
        session.add(db_file)
        if folder is not None:
            folder.file_count = int(folder.file_count or 0) + 1
            session.add(folder)
        await session.flush()
        await session.refresh(db_file)

    from StAgent.tasks.jobs import enqueue_file_processing

    await enqueue_file_processing(
        db,
        file_id=str(file_id),
        storage_path=storage_path,
        mime_type=file.content_type,
        original_name=file.filename,
        user_id=user_id,
        session_id=session_id,
    )

    return DocumentUploadResponse(
        id=db_file.id,
        name=db_file.name,
        original_name=db_file.original_name,
        file_type=db_file.file_type,
        mime_type=db_file.mime_type,
        size=db_file.size,
        status=db_file.status,
    )


@router.post("/promote", response_model=PromoteToKnowledgeResponse)
async def promote_to_knowledge(
    user_id: CurrentUserId,
    db: Annotated[DatabaseService, Depends(get_database_service)],
    body: PromoteToKnowledgeRequest,
) -> PromoteToKnowledgeResponse:
    """Copy owned chat/workspace files into the system knowledge directory.

    Does not move or delete the source rows; creates new :class:`FileModel` rows
    under ``…/knowledge/system/`` (and schedules the same post-processing as
    chat uploads). Sources that already live in the system knowledge store are
    skipped.
    """
    from StAgent.knowledge.acl import ensure_personal_knowledge_space
    from StAgent.tasks.jobs import enqueue_file_processing  # noqa: PLC0415

    promoted: list[DocumentUploadResponse] = []
    skipped: list[SkippedPromoteFile] = []

    async with db.session() as session:
        if body.session_id is not None:
            cs = await load_entity_by_id(
                session,
                ChatSession,
                body.session_id,
                parent_table="chat_sessions",
            )
            if cs is None or not same_user_id(cs.user_id, user_id):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Invalid session_id for the current user",
                )

    personal_space_id = await ensure_personal_knowledge_space(db, user_id)

    for raw_id in body.file_ids:
        async with db.session() as session:
            src = await load_entity_by_id(
                session,
                FileModel,
                raw_id,
                parent_table="files",
            )
            if src is None or src.is_deleted:
                skipped.append(SkippedPromoteFile(id=raw_id, reason="not_found"))
                continue
            if not same_user_id(src.user_id, user_id):
                skipped.append(SkippedPromoteFile(id=raw_id, reason="forbidden"))
                continue
            if src.library_scope == LibraryScope.KNOWLEDGE:
                skipped.append(SkippedPromoteFile(id=raw_id, reason="already_in_knowledge"))
                continue
            if src.size > MAX_FILE_SIZE:
                skipped.append(SkippedPromoteFile(id=raw_id, reason="too_large"))
                continue

            from StAgent.api.v1.files import (
                _load_owned_folder_for_file_write,
                _read_managed_file_bytes,
                _store_blob,
            )

            if str(src.storage_path or "").startswith("s3://"):
                try:
                    content, _content_type = await _read_managed_file_bytes(src, db)
                except (FileNotFoundError, RuntimeError):
                    skipped.append(SkippedPromoteFile(id=raw_id, reason="missing_on_storage"))
                    continue
            elif not src.storage_path or not os.path.isfile(src.storage_path):
                skipped.append(SkippedPromoteFile(id=raw_id, reason="missing_on_disk"))
                continue
            else:
                with open(src.storage_path, "rb") as rf:
                    content = rf.read()

            folder = None
            folder_id = None
            if src.folder_id is not None:
                try:
                    folder = await _load_owned_folder_for_file_write(
                        session,
                        src.folder_id,
                        user_id,
                    )
                    folder_id = src.folder_id
                except ValueError:
                    folder = None
                    folder_id = None

            ref = await _store_blob(
                content,
                filename=src.original_name,
                content_type=src.mime_type,
                user_id=user_id,
                session_id=body.session_id,
                library_scope=LibraryScope.KNOWLEDGE.value,
                origin_type="promote",
                origin_ref=str(raw_id),
                category="knowledge_promote",
            )
            file_id = ref.id
            safe_name = os.path.basename(ref.storage_key)
            storage_path = ref.metadata.get("storage_path", ref.storage_key)
            checksum = ref.checksum

            db_new = FileModel(
                id=file_id,
                session_id=body.session_id,
                name=safe_name,
                original_name=src.original_name,
                file_type=src.file_type,
                mime_type=src.mime_type,
                size=len(content),
                status=FileStatus.UPLOADED,
                user_id=user_id,
                workspace_id=user_id,
                knowledge_space_id=personal_space_id,
                access_scope="private",
                folder_id=folder_id,
                storage_path=storage_path,
                checksum=checksum,
                library_scope=LibraryScope.KNOWLEDGE,
                inbox_state=InboxState.NEW,
                origin_type="promote",
                origin_ref=str(raw_id),
                is_indexed=False,
            )
            session.add(db_new)
            if folder is not None:
                folder.file_count = int(folder.file_count or 0) + 1
                session.add(folder)
            await session.flush()
            await session.refresh(db_new)

            promoted.append(
                DocumentUploadResponse(
                    id=db_new.id,
                    name=db_new.name,
                    original_name=db_new.original_name,
                    file_type=db_new.file_type,
                    mime_type=db_new.mime_type,
                    size=db_new.size,
                    status=db_new.status,
                )
            )

            await enqueue_file_processing(
                db,
                file_id=str(file_id),
                storage_path=storage_path,
                mime_type=db_new.mime_type,
                original_name=db_new.original_name,
                user_id=user_id,
            )

    return PromoteToKnowledgeResponse(promoted=promoted, skipped=skipped)


@router.get("", response_model=PaginatedResponse[FileRead])
async def list_documents(
    user_id: CurrentUserId,
    db: Annotated[DatabaseService, Depends(get_database_service)],
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    file_type: FileType | None = Query(default=None),
    status: FileStatus | None = Query(default=None),
    folder_id: UUID | None = Query(default=None),
    space_id: UUID | None = None,
    unfiled: bool = Query(
        default=False,
        description="When true and folder_id is omitted, return only documents with folder_id IS NULL.",
    ),
    search: str | None = Query(default=None, max_length=100),
) -> PaginatedResponse[FileRead]:
    """List documents for the current user with pagination and filters."""
    async with db.session() as session:
        pet_lib = _pet_project_library_file_ids_subquery(session, user_id)
        query = select(FileModel).where(
            knowledge_file_access_clause(session, user_id),
            FileModel.is_deleted == False,  # noqa: E712
            FileModel.library_scope == LibraryScope.KNOWLEDGE,
            ~FileModel.id.in_(pet_lib),
        )

        if file_type is not None:
            query = query.where(FileModel.file_type == file_type)
        if status is not None:
            query = query.where(FileModel.status == status)
        if folder_id is not None:
            query = query.where(uuid_column_equals(session, FileModel.folder_id, folder_id))
        elif unfiled:
            query = query.where(FileModel.folder_id == None)  # noqa: E711
        if space_id is not None:
            query = query.where(uuid_column_equals(session, FileModel.knowledge_space_id, space_id))
        if search:
            query = query.where(FileModel.original_name.ilike(f"%{search}%"))

        count_query = select(func.count()).select_from(query.subquery())
        count_result = await session.exec(count_query)
        total = count_result.one()

        query = query.order_by(col(FileModel.created_at).desc())
        query = query.offset((page - 1) * page_size).limit(page_size)

        result = await session.exec(query)
        files = list(result.all())

        return PaginatedResponse[FileRead](
            items=[FileRead.model_validate(f) for f in files],
            total=total,
            page=page,
            page_size=page_size,
            has_next=(page * page_size) < total,
            has_prev=page > 1,
        )


@router.get("/search", response_model=DocumentSearchResponse)
async def search_documents(
    user_id: CurrentUserId,
    db: Annotated[DatabaseService, Depends(get_database_service)],
    query: str = Query(..., min_length=1, max_length=1000),
    file_types: str | None = Query(default=None, description="Comma-separated file types"),
    folder_id: UUID | None = Query(default=None),
    space_id: UUID | None = None,
    limit: int = Query(default=10, ge=1, le=100),
) -> DocumentSearchResponse:
    """BM25/lexical search across knowledge document chunks."""
    type_filter: list[FileType] | None = None
    if file_types:
        type_filter = [FileType(t.strip()) for t in file_types.split(",") if t.strip()]

    from StAgent.library.retrieval import (
        KnowledgeRetrievalPipeline,
        SearchOptions,
        get_knowledge_retrieval_pipeline,
    )

    pipeline = get_knowledge_retrieval_pipeline() or KnowledgeRetrievalPipeline(db)
    retrieval_hits = await pipeline.search(
        SearchOptions(
            query=query,
            user_id=user_id,
            # Retrieve a broad bounded candidate pool before collapsing chunks
            # to documents; otherwise one long document can crowd shared
            # documents out of the page.
            limit=max(limit * 4, 100),
            library_scope=LibraryScope.KNOWLEDGE.value,
            file_type_filter=[item.value for item in type_filter] if type_filter else None,
            folder_id=folder_id,
            space_id=space_id,
        )
    )
    best_hits: dict[UUID, Any] = {}
    for hit in retrieval_hits:
        previous = best_hits.get(hit.file_id)
        if previous is None or hit.score > previous.score:
            best_hits[hit.file_id] = hit

    search_results: list[DocumentSearchResult] = []
    ranked_hits = sorted(best_hits.values(), key=lambda item: item.score, reverse=True)
    # Batch load all file metadata in a single query instead of N+1
    file_ids = [hit.file_id for hit in ranked_hits]
    files_by_id: dict[UUID, Any] = {}
    if file_ids:
        async with db.session() as session:
            batch_stmt = (
                select(FileModel)
                .where(FileModel.is_deleted == False)  # noqa: E712
                .where(knowledge_file_access_clause(session, user_id))
                .where(FileModel.library_scope == LibraryScope.KNOWLEDGE)
                .where(FileModel.is_indexed == True)  # noqa: E712
                .where(uuid_column_in(session, FileModel.id, file_ids))
            )
            result = await session.exec(batch_stmt)
            for f in result.all():
                files_by_id[f.id] = f

    for hit in ranked_hits:
        f = files_by_id.get(hit.file_id)
        if f is None:
            continue
        if type_filter and f.file_type not in type_filter:
            continue
        if folder_id is not None and not same_user_id(f.folder_id, folder_id):
            continue
        search_results.append(
            DocumentSearchResult(
                id=hit.file_id,
                name=f.original_name,
                file_type=f.file_type,
                mime_type=f.mime_type,
                size=f.size,
                status=f.status,
                is_indexed=f.is_indexed,
                summary=f.summary,
                created_at=f.created_at,
                folder_id=f.folder_id,
                score=hit.score,
                snippet=hit.snippet,
                chunk_id=hit.chunk_id,
                start_offset=hit.start_offset,
                end_offset=hit.end_offset,
            )
        )
        if len(search_results) >= limit:
            break

    return DocumentSearchResponse(
        query=query,
        results=search_results,
        total=len(search_results),
    )


@router.post("/{file_id}/reindex", response_model=DocumentReindexResponse)
async def reindex_document(
    file_id: UUID,
    user_id: CurrentUserId,
    db: Annotated[DatabaseService, Depends(get_database_service)],
) -> DocumentReindexResponse:
    """Reset and enqueue one owned knowledge document for extraction/indexing."""
    async with db.session() as session:
        file = await load_entity_by_id(
            session,
            FileModel,
            file_id,
            parent_table="files",
        )
        can_write = False
        if file is not None and not file.is_deleted and file.library_scope == LibraryScope.KNOWLEDGE:
            can_write = (
                await session.exec(
                    select(FileModel.id)
                    .where(uuid_column_equals(session, FileModel.id, file_id))
                    .where(knowledge_file_write_clause(session, user_id))
                )
            ).first() is not None
        if file is None or file.is_deleted or file.library_scope != LibraryScope.KNOWLEDGE or not can_write:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Knowledge document not found",
            )
        if not file.storage_path:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Document source file is unavailable",
            )
        if str(file.storage_path).startswith("s3://"):
            from StAgent.api.v1.files import _managed_file_exists

            if not await _managed_file_exists(file, db):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Document source file is unavailable",
                )
        elif not os.path.isfile(file.storage_path):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Document source file is unavailable",
            )

        storage_path = file.storage_path
        mime_type = file.mime_type
        original_name = file.original_name
        session_id = file.session_id
        await session.execute(
            sa_update(FileModel)
            .where(uuid_column_equals(session, FileModel.id, file_id))
            .values(
                status=FileStatus.PROCESSING,
                is_indexed=False,
                summary=None,
                updated_at=datetime.utcnow(),
            )
        )

    from StAgent.tasks.jobs import enqueue_file_processing

    try:
        task_id = await enqueue_file_processing(
            db,
            file_id=str(file_id),
            storage_path=storage_path,
            mime_type=mime_type,
            original_name=original_name,
            user_id=user_id,
            session_id=session_id,
        )
    except Exception:
        async with db.session() as session:
            await session.execute(
                sa_update(FileModel)
                .where(uuid_column_equals(session, FileModel.id, file_id))
                .values(
                    status=FileStatus.FAILED,
                    is_indexed=False,
                    updated_at=datetime.utcnow(),
                )
            )
        raise
    return DocumentReindexResponse(
        id=file_id,
        status=FileStatus.PROCESSING,
        is_indexed=False,
        task_id=task_id,
    )
