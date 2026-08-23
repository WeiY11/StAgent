"""Durable knowledge-index state and worker queue operations."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Protocol
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlmodel import select

from StAgent.db.models.base import utc_now
from StAgent.db.models.knowledge_index import (
    KnowledgeIndexJob,
    KnowledgeIndexJobStatus,
    KnowledgeIndexState,
    KnowledgeIndexStatus,
)
from StAgent.db.sqlite_compat import (
    session_dialect_name,
    sqlite_parent_id_text,
    uuid_column_equals,
)

if TYPE_CHECKING:
    from StAgent.db.service import DatabaseService


INDEX_VERSION = "rag-v1"


class KnowledgeIndexRepository(Protocol):
    async def upsert_state(
        self,
        file_id: UUID,
        user_id: UUID | None,
        *,
        status: KnowledgeIndexStatus,
        index_version: str = INDEX_VERSION,
        content_hash: str | None = None,
        chunk_count: int | None = None,
        embedded_count: int | None = None,
        attempt_count: int | None = None,
        last_error: str | None = None,
        started_at: datetime | None = None,
        completed_at: datetime | None = None,
    ) -> KnowledgeIndexState: ...

    async def enqueue(
        self,
        file_id: UUID,
        user_id: UUID | None,
        *,
        content_hash: str | None,
        index_version: str = INDEX_VERSION,
        available_at: datetime | None = None,
    ) -> UUID: ...

    async def claim_batch(self, worker_id: str, *, limit: int = 10) -> list[KnowledgeIndexJob]: ...

    async def mark_succeeded(self, job_id: UUID, *, chunk_count: int, embedded_count: int = 0) -> None: ...

    async def mark_failed(self, job_id: UUID, error: str, *, retry_after_seconds: int = 60) -> None: ...


class DbKnowledgeIndexRepository:
    """Database-backed queue with PostgreSQL row locks and SQLite fallback."""

    def __init__(self, db: DatabaseService) -> None:
        self._db = db

    async def upsert_state(
        self,
        file_id: UUID,
        user_id: UUID | None,
        *,
        status: KnowledgeIndexStatus,
        index_version: str = INDEX_VERSION,
        content_hash: str | None = None,
        chunk_count: int | None = None,
        embedded_count: int | None = None,
        attempt_count: int | None = None,
        last_error: str | None = None,
        started_at: datetime | None = None,
        completed_at: datetime | None = None,
    ) -> KnowledgeIndexState:
        now = utc_now()
        async with self._db.session() as session:
            existing = (
                await session.exec(
                    select(KnowledgeIndexState).where(
                        uuid_column_equals(session, KnowledgeIndexState.file_id, file_id)
                    )
                )
            ).first()
            if existing is None:
                state_id = uuid4()
                file_db_id = (
                    await sqlite_parent_id_text(session, "files", file_id)
                    if session_dialect_name(session) == "sqlite"
                    else str(file_id)
                )
                values = {
                    "created_at": now,
                    "updated_at": now,
                    "id": str(state_id),
                    "file_id": file_db_id,
                    "user_id": str(user_id) if user_id else None,
                    "index_version": index_version,
                    "content_hash": content_hash,
                    "status": status.value,
                    "chunk_count": chunk_count or 0,
                    "embedded_count": embedded_count or 0,
                    "attempt_count": attempt_count or 0,
                    "last_error": last_error,
                    "started_at": started_at,
                    "completed_at": completed_at,
                }
                if session_dialect_name(session) == "sqlite":
                    await session.execute(
                        sa.text(
                            "INSERT INTO knowledge_index_states "
                            "(created_at, updated_at, id, file_id, user_id, index_version, "
                            "content_hash, status, chunk_count, embedded_count, attempt_count, "
                            "last_error, started_at, completed_at) VALUES "
                            "(:created_at, :updated_at, :id, :file_id, :user_id, :index_version, "
                            ":content_hash, :status, :chunk_count, :embedded_count, :attempt_count, "
                            ":last_error, :started_at, :completed_at)"
                        ),
                        values,
                    )
                else:
                    session.add(
                        KnowledgeIndexState(
                            id=state_id,
                            file_id=file_id,
                            user_id=user_id,
                            index_version=index_version,
                            content_hash=content_hash,
                            status=status,
                            chunk_count=chunk_count or 0,
                            embedded_count=embedded_count or 0,
                            attempt_count=attempt_count or 0,
                            last_error=last_error,
                            started_at=started_at,
                            completed_at=completed_at,
                        )
                    )
                return KnowledgeIndexState(
                    id=state_id,
                    file_id=file_id,
                    user_id=user_id,
                    index_version=index_version,
                    content_hash=content_hash,
                    status=status,
                    chunk_count=chunk_count or 0,
                    embedded_count=embedded_count or 0,
                    attempt_count=attempt_count or 0,
                    last_error=last_error,
                    started_at=started_at,
                    completed_at=completed_at,
                )

            updates: dict[str, object] = {
                "updated_at": now,
                "index_version": index_version,
                "status": status.value,
            }
            if content_hash is not None:
                updates["content_hash"] = content_hash
            if chunk_count is not None:
                updates["chunk_count"] = chunk_count
            if embedded_count is not None:
                updates["embedded_count"] = embedded_count
            if attempt_count is not None:
                updates["attempt_count"] = attempt_count
            if last_error is not None or status in {
                KnowledgeIndexStatus.READY,
                KnowledgeIndexStatus.CHUNKED,
                KnowledgeIndexStatus.EMBEDDING,
            }:
                updates["last_error"] = last_error
            if started_at is not None:
                updates["started_at"] = started_at
            if completed_at is not None:
                updates["completed_at"] = completed_at
            await session.execute(
                sa.update(KnowledgeIndexState)
                .where(uuid_column_equals(session, KnowledgeIndexState.file_id, file_id))
                .values(**updates)
            )
            # The UPDATE is intentionally expression-based for dashed SQLite
            # UUIDs.  Detach the loaded row before reflecting the new values so
            # the ORM does not flush a second, stale primary-key UPDATE.
            session.expunge(existing)
            for key, value in updates.items():
                if hasattr(existing, key):
                    setattr(existing, key, value)
            return existing

    async def enqueue(
        self,
        file_id: UUID,
        user_id: UUID | None,
        *,
        content_hash: str | None,
        index_version: str = INDEX_VERSION,
        available_at: datetime | None = None,
    ) -> UUID:
        available_at = available_at or utc_now()
        async with self._db.session() as session:
            stmt = select(KnowledgeIndexJob).where(
                uuid_column_equals(session, KnowledgeIndexJob.file_id, file_id),
                KnowledgeIndexJob.index_version == index_version,
                KnowledgeIndexJob.content_hash == content_hash,
                KnowledgeIndexJob.status.in_(
                    [
                        KnowledgeIndexJobStatus.QUEUED,
                        KnowledgeIndexJobStatus.RUNNING,
                        # A failed attempt remains the durable retry record
                        # until its available_at deadline.  Reusing it keeps
                        # re-uploads idempotent and avoids creating an
                        # unbounded duplicate queue for the same content.
                        KnowledgeIndexJobStatus.FAILED,
                    ]
                ),
            )
            existing = (await session.exec(stmt)).first()
            if existing is not None:
                return existing.id

            job_id = uuid4()
            file_db_id = (
                await sqlite_parent_id_text(session, "files", file_id)
                if session_dialect_name(session) == "sqlite"
                else str(file_id)
            )
            values = {
                "created_at": available_at,
                "updated_at": available_at,
                "id": str(job_id),
                "file_id": file_db_id,
                "user_id": str(user_id) if user_id else None,
                "index_version": index_version,
                "content_hash": content_hash,
                "status": KnowledgeIndexJobStatus.QUEUED.value,
                "available_at": available_at,
                "locked_at": None,
                "locked_by": None,
                "attempt_count": 0,
                "last_error": None,
            }
            if session_dialect_name(session) == "sqlite":
                await session.execute(
                    sa.text(
                        "INSERT INTO knowledge_index_jobs "
                        "(created_at, updated_at, id, file_id, user_id, index_version, "
                        "content_hash, status, available_at, locked_at, locked_by, "
                        "attempt_count, last_error) VALUES (:created_at, :updated_at, :id, "
                        ":file_id, :user_id, :index_version, :content_hash, :status, "
                        ":available_at, :locked_at, :locked_by, :attempt_count, :last_error)"
                    ),
                    values,
                )
            else:
                session.add(
                    KnowledgeIndexJob(
                        id=job_id,
                        file_id=file_id,
                        user_id=user_id,
                        index_version=index_version,
                        content_hash=content_hash,
                        status=KnowledgeIndexJobStatus.QUEUED,
                        available_at=available_at,
                    )
                )
            return job_id

    async def claim_batch(self, worker_id: str, *, limit: int = 10) -> list[KnowledgeIndexJob]:
        now = utc_now()
        async with self._db.session() as session:
            stmt = (
                select(KnowledgeIndexJob)
                .where(
                    KnowledgeIndexJob.status.in_(
                        [KnowledgeIndexJobStatus.QUEUED, KnowledgeIndexJobStatus.FAILED]
                    ),
                    KnowledgeIndexJob.available_at <= now,
                )
                .order_by(KnowledgeIndexJob.available_at, KnowledgeIndexJob.created_at)
                .limit(max(1, min(limit, 100)))
            )
            if session_dialect_name(session) == "postgresql":
                stmt = stmt.with_for_update(skip_locked=True)
            jobs = list((await session.exec(stmt)).all())
            claimed: list[KnowledgeIndexJob] = []
            for job in jobs:
                locked_at = now
                next_attempt = int(job.attempt_count or 0) + 1
                await session.execute(
                    sa.update(KnowledgeIndexJob)
                    .where(uuid_column_equals(session, KnowledgeIndexJob.id, job.id))
                    .values(
                        status=KnowledgeIndexJobStatus.RUNNING.value,
                        locked_at=locked_at,
                        locked_by=worker_id,
                        attempt_count=next_attempt,
                        updated_at=now,
                    )
                )
                session.expunge(job)
                job.status = KnowledgeIndexJobStatus.RUNNING
                job.locked_at = locked_at
                job.locked_by = worker_id
                job.attempt_count = next_attempt
                claimed.append(job)
            return claimed

    async def mark_succeeded(self, job_id: UUID, *, chunk_count: int, embedded_count: int = 0) -> None:
        now = utc_now()
        async with self._db.session() as session:
            job = (
                await session.exec(
                    select(KnowledgeIndexJob).where(
                        uuid_column_equals(session, KnowledgeIndexJob.id, job_id)
                    )
                )
            ).first()
            if job is None:
                return
            await session.execute(
                sa.update(KnowledgeIndexJob)
                .where(uuid_column_equals(session, KnowledgeIndexJob.id, job_id))
                .values(status=KnowledgeIndexJobStatus.SUCCEEDED.value, updated_at=now)
            )
            await session.execute(
                sa.update(KnowledgeIndexState)
                .where(_state_matches_job(session, job))
                .values(
                    status=KnowledgeIndexStatus.READY.value,
                    chunk_count=chunk_count,
                    embedded_count=embedded_count,
                    last_error=None,
                    completed_at=now,
                    updated_at=now,
                )
            )

    async def mark_failed(self, job_id: UUID, error: str, *, retry_after_seconds: int = 60) -> None:
        now = utc_now()
        retry_at = now + timedelta(seconds=max(1, retry_after_seconds))
        async with self._db.session() as session:
            job = (
                await session.exec(
                    select(KnowledgeIndexJob).where(
                        uuid_column_equals(session, KnowledgeIndexJob.id, job_id)
                    )
                )
            ).first()
            if job is None:
                return
            await session.execute(
                sa.update(KnowledgeIndexJob)
                .where(uuid_column_equals(session, KnowledgeIndexJob.id, job_id))
                .values(
                    status=KnowledgeIndexJobStatus.FAILED.value,
                    last_error=error[:4000],
                    available_at=retry_at,
                    updated_at=now,
                )
            )
            await session.execute(
                sa.update(KnowledgeIndexState)
                .where(_state_matches_job(session, job))
                .values(
                    status=KnowledgeIndexStatus.FAILED.value,
                    last_error=error[:4000],
                    updated_at=now,
                )
            )


def _state_matches_job(session: object, job: KnowledgeIndexJob) -> sa.ColumnElement[bool]:
    """Restrict job completion updates to the same index projection version."""
    predicate = (
        uuid_column_equals(session, KnowledgeIndexState.file_id, job.file_id)
        & (KnowledgeIndexState.index_version == job.index_version)
    )
    if job.content_hash is not None:
        predicate = predicate & (KnowledgeIndexState.content_hash == job.content_hash)
    return predicate


__all__ = ["INDEX_VERSION", "KnowledgeIndexRepository", "DbKnowledgeIndexRepository"]
