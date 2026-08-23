"""Knowledge-space and document ACL management endpoints."""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime  # noqa: TC003 - Pydantic resolves response annotations at runtime
from uuid import UUID  # noqa: TC003 - FastAPI resolves endpoint annotations at runtime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete as sa_delete
from sqlalchemy import or_
from sqlalchemy import update as sa_update
from sqlmodel import select

from StAgent.db import DatabaseService, get_database_service
from StAgent.db.models import (
    File,
    FileStatus,
    KnowledgeAccessScope,
    KnowledgeDocumentGrant,
    KnowledgeIndexState,
    KnowledgeIndexStatus,
    KnowledgeSpace,
    KnowledgeSpaceCreate,
    KnowledgeSpaceMember,
    KnowledgeSpaceMemberRead,
    KnowledgeSpaceMemberRole,
    KnowledgeSpaceRead,
    LibraryScope,
    UserStub,
    WorkspaceMember,
    WorkspaceMemberRole,
)
from StAgent.db.sqlite_compat import (
    load_entity_by_id,
    same_user_id,
    uuid_column_equals,
    uuid_columns_equal,
)
from StAgent.knowledge.acl import ensure_personal_knowledge_space
from StAgent.services.auth import CurrentUserId  # noqa: TC001 - FastAPI dependency alias

router = APIRouter()
logger = logging.getLogger(__name__)


class SpaceMemberUpdate(BaseModel):
    role: KnowledgeSpaceMemberRole = KnowledgeSpaceMemberRole.VIEWER


class DocumentAclUpdate(BaseModel):
    space_id: UUID | None = None
    access_scope: KnowledgeAccessScope = KnowledgeAccessScope.PRIVATE
    grant_user_ids: list[UUID] = Field(default_factory=list, max_length=2000)


class DocumentAclRead(BaseModel):
    file_id: UUID
    space_id: UUID | None
    access_scope: KnowledgeAccessScope
    grants: list[UUID]


class RetrievalEvaluationCaseBody(BaseModel):
    query: str = Field(min_length=1, max_length=4000)
    relevant_chunk_ids: list[str] = Field(min_length=1, max_length=500)
    case_id: str = Field(default="", max_length=160)


class RetrievalEvaluationRequest(BaseModel):
    cases: list[RetrievalEvaluationCaseBody] = Field(min_length=1, max_length=500)
    k: int = Field(default=10, ge=1, le=100)
    query_rewrite: bool = False
    rerank: bool = True
    citation_accuracy: float | None = Field(default=None, ge=0.0, le=1.0)
    hallucination_rate: float | None = Field(default=None, ge=0.0, le=1.0)


class KnowledgeIndexStatusRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    file_id: UUID
    index_version: str
    content_hash: str | None
    status: str
    chunk_count: int
    embedded_count: int
    attempt_count: int
    last_error: str | None
    started_at: datetime | None = None
    completed_at: datetime | None = None


class KnowledgeBulkReindexRequest(BaseModel):
    """Bounded request to rebuild documents stale for the active RAG version."""

    limit: int = Field(default=100, ge=1, le=500)


class KnowledgeBulkReindexResponse(BaseModel):
    """Accepted and skipped counts for a bounded stale-projection rebuild."""

    index_version: str
    matched: int
    accepted: int
    unavailable: int


async def _can_manage_space(session: object, user_id: UUID, space_id: UUID) -> bool:
    """Return true for an org owner/admin or space manager."""
    workspace_owner = (
        await session.exec(  # type: ignore[attr-defined]
            select(WorkspaceMember)
            .join(
                KnowledgeSpace,
                uuid_columns_equal(
                    session,
                    KnowledgeSpace.workspace_id,
                    WorkspaceMember.workspace_id,
                ),
            )
            .where(
                uuid_column_equals(session, KnowledgeSpace.id, space_id),  # type: ignore[arg-type]
                uuid_column_equals(session, WorkspaceMember.user_id, user_id),  # type: ignore[arg-type]
                WorkspaceMember.role.in_([WorkspaceMemberRole.OWNER, WorkspaceMemberRole.ADMIN]),
            )
        )
    ).first()
    if workspace_owner is not None:
        return True
    manager = (
        await session.exec(  # type: ignore[attr-defined]
            select(KnowledgeSpaceMember).where(
                uuid_column_equals(session, KnowledgeSpaceMember.space_id, space_id),  # type: ignore[arg-type]
                uuid_column_equals(session, KnowledgeSpaceMember.user_id, user_id),  # type: ignore[arg-type]
                KnowledgeSpaceMember.role == KnowledgeSpaceMemberRole.MANAGER,
            )
        )
    ).first()
    return manager is not None


@router.get("/spaces", response_model=list[KnowledgeSpaceRead])
async def list_knowledge_spaces(
    user_id: CurrentUserId,
    db: DatabaseService = Depends(get_database_service),
) -> list[KnowledgeSpaceRead]:
    """List spaces visible to the current organization member."""
    await ensure_personal_knowledge_space(db, user_id)
    async with db.session() as session:
        rows = (
            await session.exec(
                select(KnowledgeSpace)
                .join(
                    WorkspaceMember,
                    uuid_columns_equal(
                        session,
                        KnowledgeSpace.workspace_id,
                        WorkspaceMember.workspace_id,
                    ),
                )
                .where(
                    KnowledgeSpace.is_deleted == False,  # noqa: E712
                    uuid_column_equals(session, WorkspaceMember.user_id, user_id),
                )
                .distinct()
                .order_by(KnowledgeSpace.name)
            )
        ).all()
        return [KnowledgeSpaceRead.model_validate(row) for row in rows]


@router.post("/spaces", response_model=KnowledgeSpaceRead, status_code=status.HTTP_201_CREATED)
async def create_knowledge_space(
    body: KnowledgeSpaceCreate,
    user_id: CurrentUserId,
    db: DatabaseService = Depends(get_database_service),
) -> KnowledgeSpaceRead:
    """Create a space in the caller's organization."""
    await ensure_personal_knowledge_space(db, user_id)
    slug = (body.slug or body.name).strip().lower()
    slug = re.sub(r"[^a-z0-9\u4e00-\u9fff_-]+", "-", slug).strip("-")[:160]
    if not slug:
        raise HTTPException(status_code=400, detail="Space slug is required")
    async with db.session() as session:
        existing = (
            await session.exec(
                select(KnowledgeSpace).where(
                    uuid_column_equals(session, KnowledgeSpace.workspace_id, user_id),
                    KnowledgeSpace.slug == slug,
                    KnowledgeSpace.is_deleted == False,  # noqa: E712
                )
            )
        ).first()
        if existing is not None:
            raise HTTPException(status_code=409, detail="Space slug already exists")
        space = KnowledgeSpace(
            workspace_id=user_id,
            name=body.name.strip(),
            slug=slug,
            description=body.description,
            default_access_scope=body.default_access_scope,
        )
        session.add(space)
        await session.flush()
        session.add(
            KnowledgeSpaceMember(
                space_id=space.id,
                user_id=user_id,
                role=KnowledgeSpaceMemberRole.MANAGER,
            )
        )
        return KnowledgeSpaceRead.model_validate(space)


@router.get("/spaces/{space_id}/members", response_model=list[KnowledgeSpaceMemberRead])
async def list_space_members(
    space_id: UUID,
    user_id: CurrentUserId,
    db: DatabaseService = Depends(get_database_service),
) -> list[KnowledgeSpaceMemberRead]:
    async with db.session() as session:
        if not await _can_manage_space(session, user_id, space_id):
            raise HTTPException(status_code=403, detail="Space management permission required")
        rows = (
            await session.exec(
                select(KnowledgeSpaceMember).where(uuid_column_equals(session, KnowledgeSpaceMember.space_id, space_id))
            )
        ).all()
        return [KnowledgeSpaceMemberRead.model_validate(row) for row in rows]


@router.put("/spaces/{space_id}/members/{target_user_id}", response_model=KnowledgeSpaceMemberRead)
async def update_space_member(
    space_id: UUID,
    target_user_id: UUID,
    body: SpaceMemberUpdate,
    user_id: CurrentUserId,
    db: DatabaseService = Depends(get_database_service),
) -> KnowledgeSpaceMemberRead:
    async with db.session() as session:
        if not await _can_manage_space(session, user_id, space_id):
            raise HTTPException(status_code=403, detail="Space management permission required")
        target = await session.get(UserStub, target_user_id)
        if target is None:
            raise HTTPException(status_code=404, detail="Target user not found")
        membership = (
            await session.exec(
                select(KnowledgeSpaceMember).where(
                    uuid_column_equals(session, KnowledgeSpaceMember.space_id, space_id),
                    uuid_column_equals(session, KnowledgeSpaceMember.user_id, target_user_id),
                )
            )
        ).first()
        if membership is None:
            membership = KnowledgeSpaceMember(
                space_id=space_id,
                user_id=target_user_id,
                role=body.role,
            )
            session.add(membership)
        else:
            membership.role = body.role
            session.add(membership)
        await session.flush()
        return KnowledgeSpaceMemberRead.model_validate(membership)


@router.get("/documents/{file_id}/acl", response_model=DocumentAclRead)
async def get_document_acl(
    file_id: UUID,
    user_id: CurrentUserId,
    db: DatabaseService = Depends(get_database_service),
) -> DocumentAclRead:
    async with db.session() as session:
        file = await load_entity_by_id(session, File, file_id, parent_table="files")
        if file is None or file.is_deleted or file.library_scope != LibraryScope.KNOWLEDGE:
            raise HTTPException(status_code=404, detail="Knowledge document not found")
        can_manage = same_user_id(file.user_id, user_id)
        if not can_manage and file.knowledge_space_id is not None:
            can_manage = await _can_manage_space(session, user_id, file.knowledge_space_id)
        if not can_manage:
            raise HTTPException(status_code=404, detail="Knowledge document not found")
        grants = (
            await session.exec(
                select(KnowledgeDocumentGrant.user_id).where(
                    uuid_column_equals(session, KnowledgeDocumentGrant.file_id, file_id)
                )
            )
        ).all()
        return DocumentAclRead(
            file_id=file_id,
            space_id=file.knowledge_space_id,
            access_scope=file.access_scope or KnowledgeAccessScope.PRIVATE,
            grants=list(grants),
        )


@router.put("/documents/{file_id}/acl", response_model=DocumentAclRead)
async def update_document_acl(
    file_id: UUID,
    body: DocumentAclUpdate,
    user_id: CurrentUserId,
    db: DatabaseService = Depends(get_database_service),
) -> DocumentAclRead:
    """Change visibility and explicit grants without exposing file contents."""
    async with db.session() as session:
        file = await load_entity_by_id(session, File, file_id, parent_table="files")
        if (
            file is None
            or file.is_deleted
            or file.library_scope != LibraryScope.KNOWLEDGE
            or not (
                same_user_id(file.user_id, user_id)
                or (
                    file.knowledge_space_id is not None
                    and await _can_manage_space(session, user_id, file.knowledge_space_id)
                )
            )
        ):
            raise HTTPException(status_code=404, detail="Knowledge document not found")
        if body.access_scope != KnowledgeAccessScope.PRIVATE and body.space_id is None:
            raise HTTPException(status_code=400, detail="space_id is required for shared access")
        if body.space_id is not None:
            space = await session.get(KnowledgeSpace, body.space_id)
            if space is None or space.is_deleted:
                raise HTTPException(status_code=404, detail="Knowledge space not found")
            if not await _can_manage_space(session, user_id, body.space_id):
                raise HTTPException(status_code=403, detail="Space management permission required")
        space_workspace_id = None
        if body.space_id is not None:
            space_row = await session.get(KnowledgeSpace, body.space_id)
            space_workspace_id = space_row.workspace_id if space_row is not None else None
        await session.execute(
            sa_update(File)
            .where(uuid_column_equals(session, File.id, file_id))
            .values(
                workspace_id=space_workspace_id,
                knowledge_space_id=body.space_id,
                access_scope=body.access_scope.value,
            )
        )
        await session.execute(
            sa_delete(KnowledgeDocumentGrant).where(
                uuid_column_equals(session, KnowledgeDocumentGrant.file_id, file_id)
            )
        )
        if body.access_scope == KnowledgeAccessScope.RESTRICTED:
            for target_user_id in set(body.grant_user_ids):
                session.add(KnowledgeDocumentGrant(file_id=file_id, user_id=target_user_id, permission="read"))
        return DocumentAclRead(
            file_id=file_id,
            space_id=body.space_id,
            access_scope=body.access_scope,
            grants=list(set(body.grant_user_ids)) if body.access_scope == KnowledgeAccessScope.RESTRICTED else [],
        )


@router.get("/index-status", response_model=list[KnowledgeIndexStatusRead])
async def list_knowledge_index_status(
    user_id: CurrentUserId,
    db: DatabaseService = Depends(get_database_service),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[KnowledgeIndexStatusRead]:
    """List index lifecycle state for documents visible to the caller."""
    from StAgent.knowledge.acl import knowledge_file_access_clause

    async with db.session() as session:
        rows = (
            await session.exec(
                select(KnowledgeIndexState)
                .join(
                    File,
                    uuid_columns_equal(session, KnowledgeIndexState.file_id, File.id),
                )
                .where(
                    File.is_deleted == False,  # noqa: E712
                    File.library_scope == LibraryScope.KNOWLEDGE,
                    knowledge_file_access_clause(session, user_id),
                )
                .order_by(KnowledgeIndexState.updated_at.desc())
                .limit(limit)
            )
        ).all()
        return [KnowledgeIndexStatusRead.model_validate(row) for row in rows]


async def _source_available_for_reindex(file: File, db: DatabaseService) -> bool:
    """Check a managed source before queueing a potentially expensive rebuild."""
    if not file.storage_path:
        return False
    if not str(file.storage_path).startswith("s3://"):
        return os.path.isfile(file.storage_path)
    try:
        from StAgent.api.v1.files import _managed_file_exists

        return await _managed_file_exists(file, db)
    except Exception:  # noqa: BLE001 - treat an unreachable blob store as unavailable
        logger.warning(
            "knowledge_bulk_reindex_source_check_failed file_id=%s",
            file.id,
            exc_info=True,
        )
        return False


async def _mark_bulk_reindex_failed(
    db: DatabaseService,
    file: File,
    *,
    index_version: str,
    error: str,
) -> None:
    """Expose an unqueueable stale projection as failed instead of ready."""
    async with db.session() as session:
        await session.execute(
            sa_update(File)
            .where(uuid_column_equals(session, File.id, file.id))
            .values(
                status=FileStatus.FAILED,
                is_indexed=False,
                updated_at=datetime.utcnow(),
            )
        )
    try:
        await db.repositories.knowledge_index.upsert_state(
            file.id,
            file.user_id,
            status=KnowledgeIndexStatus.FAILED,
            index_version=index_version,
            last_error=error,
        )
    except Exception:  # noqa: BLE001 - legacy databases may lack index-state tables
        logger.debug(
            "knowledge_bulk_reindex_failure_state_skipped file_id=%s",
            file.id,
            exc_info=True,
        )


@router.post("/reindex-stale", response_model=KnowledgeBulkReindexResponse)
async def reindex_stale_knowledge_documents(
    body: KnowledgeBulkReindexRequest,
    user_id: CurrentUserId,
    db: DatabaseService = Depends(get_database_service),
) -> KnowledgeBulkReindexResponse:
    """Queue one bounded batch of writable documents stale for the active RAG version.

    Changing ``StAgent_KNOWLEDGE_INDEX_VERSION`` intentionally creates a new
    projection.  This explicit, ACL-scoped operation backfills that projection
    without silently scheduling an unbounded startup job.
    """
    from StAgent.config.settings import get_settings
    from StAgent.knowledge.acl import knowledge_file_write_clause

    index_version = get_settings().knowledge.index_version
    async with db.session() as session:
        stale_projection = or_(
            KnowledgeIndexState.file_id.is_(None),
            KnowledgeIndexState.index_version != index_version,
            KnowledgeIndexState.status != KnowledgeIndexStatus.READY,
        )
        candidates = (
            await session.exec(
                select(File)
                .outerjoin(
                    KnowledgeIndexState,
                    uuid_columns_equal(session, KnowledgeIndexState.file_id, File.id),
                )
                .where(
                    File.is_deleted == False,  # noqa: E712
                    File.library_scope == LibraryScope.KNOWLEDGE,
                    File.status != FileStatus.PROCESSING,
                    stale_projection,
                    knowledge_file_write_clause(session, user_id),
                )
                .order_by(File.updated_at.asc())
                .limit(body.limit)
            )
        ).all()

    # Object storage checks can incur network latency.  Do them outside the
    # database transaction, then use a conditional update below to retain a
    # per-file compare-and-set guard against concurrent bulk requests.
    unavailable_files: list[File] = []
    source_ready_files: list[File] = []
    for file in candidates:
        if await _source_available_for_reindex(file, db):
            source_ready_files.append(file)
        else:
            unavailable_files.append(file)

    accepted_files: list[File] = []
    async with db.session() as session:
        for file in source_ready_files:
            update_result = await session.execute(
                sa_update(File)
                .where(
                    uuid_column_equals(session, File.id, file.id),
                    File.status != FileStatus.PROCESSING,
                )
                .values(
                    status=FileStatus.PROCESSING,
                    is_indexed=False,
                    summary=None,
                    updated_at=datetime.utcnow(),
                )
            )
            if getattr(update_result, "rowcount", 1) != 0:
                accepted_files.append(file)

    for file in unavailable_files:
        await _mark_bulk_reindex_failed(
            db,
            file,
            index_version=index_version,
            error="reindex_source_unavailable",
        )

    accepted = 0
    for file in accepted_files:
        try:
            from StAgent.tasks.jobs import enqueue_file_processing

            await enqueue_file_processing(
                db,
                file_id=str(file.id),
                storage_path=file.storage_path,
                mime_type=file.mime_type,
                original_name=file.original_name,
                user_id=file.user_id,
                session_id=file.session_id,
            )
            accepted += 1
        except Exception:  # noqa: BLE001 - repair catalog/state after a queue failure
            logger.warning(
                "knowledge_bulk_reindex_enqueue_failed file_id=%s",
                file.id,
                exc_info=True,
            )
            await _mark_bulk_reindex_failed(
                db,
                file,
                index_version=index_version,
                error="reindex_enqueue_failed",
            )

    return KnowledgeBulkReindexResponse(
        index_version=index_version,
        matched=len(candidates),
        accepted=accepted,
        unavailable=len(unavailable_files),
    )


@router.post("/evaluate")
async def evaluate_knowledge_retrieval(
    body: RetrievalEvaluationRequest,
    user_id: CurrentUserId,
    db: DatabaseService = Depends(get_database_service),
) -> dict[str, object]:
    """Run an ACL-scoped retrieval evaluation dataset.

    The endpoint reports retrieval quality and latency.  A strict answer gate
    is intentionally false unless separately judged citation/hallucination
    values are supplied, so a retrieval-only dataset cannot masquerade as an
    end-to-end grounding proof.
    """
    from StAgent.config.settings import get_settings
    from StAgent.knowledge.evaluation import RetrievalEvaluationCase, evaluate_retriever
    from StAgent.library.retrieval import (
        KnowledgeRetrievalPipeline,
        SearchOptions,
        get_knowledge_retrieval_pipeline,
    )

    pipeline = get_knowledge_retrieval_pipeline() or KnowledgeRetrievalPipeline(db)
    cases = [
        RetrievalEvaluationCase(
            query=item.query.strip(),
            relevant_chunk_ids=frozenset(value.strip() for value in item.relevant_chunk_ids if value.strip()),
            case_id=item.case_id,
        )
        for item in body.cases
    ]
    if any(not case.relevant_chunk_ids for case in cases):
        raise HTTPException(status_code=400, detail="Every case needs a relevant chunk id")

    async def retrieve(query: str, k: int) -> list[object]:
        return await pipeline.search(
            SearchOptions(
                query=query,
                user_id=user_id,
                limit=k,
                query_rewrite=body.query_rewrite,
                rerank=body.rerank,
            )
        )

    cfg = get_settings().knowledge
    report = await evaluate_retriever(
        cases,
        retrieve,
        k=body.k,
        recall_target=cfg.recall_at_10_target,
        ndcg_target=cfg.ndcg_at_10_target,
        p95_ms_target=cfg.retrieval_p95_ms_target,
        citation_accuracy=body.citation_accuracy,
        hallucination_rate=body.hallucination_rate,
        citation_accuracy_target=cfg.citation_accuracy_target,
        hallucination_rate_max=cfg.hallucination_rate_max,
    )
    return report.to_dict()
