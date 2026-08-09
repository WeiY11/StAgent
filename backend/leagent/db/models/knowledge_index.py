"""Durable state and work items for knowledge indexing.

The file catalog remains the source of truth for the uploaded artifact.  These
tables track the versioned RAG projection separately so an interrupted
extraction, embedding outage, or worker restart is visible and retryable.
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 - SQLModel resolves datetime fields at runtime
from enum import StrEnum
from uuid import UUID  # noqa: TC003 - SQLModel resolves UUID fields at runtime

from sqlmodel import Column, Field, SQLModel, Text

from leagent.db.models.base import BaseModel, enum_value_column


class KnowledgeIndexStatus(StrEnum):
    """Lifecycle state of a file's current RAG projection."""

    UPLOADED = "uploaded"
    EXTRACTING = "extracting"
    CHUNKED = "chunked"
    EMBEDDING = "embedding"
    READY = "ready"
    DEGRADED = "degraded"
    FAILED = "failed"


class KnowledgeIndexJobStatus(StrEnum):
    """Queue state for a durable indexing attempt."""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class KnowledgeIndexState(BaseModel, table=True):
    """Latest versioned indexing state for one file."""

    __tablename__ = "knowledge_index_states"

    file_id: UUID = Field(foreign_key="files.id", unique=True, index=True)
    user_id: UUID | None = Field(default=None, index=True)
    index_version: str = Field(default="rag-v1", max_length=64, index=True)
    content_hash: str | None = Field(default=None, max_length=64)
    status: KnowledgeIndexStatus = Field(
        default=KnowledgeIndexStatus.UPLOADED,
        sa_column=enum_value_column(KnowledgeIndexStatus, index=True),
    )
    chunk_count: int = Field(default=0)
    embedded_count: int = Field(default=0)
    attempt_count: int = Field(default=0)
    last_error: str | None = Field(default=None, sa_column=Column(Text))
    started_at: datetime | None = Field(default=None)
    completed_at: datetime | None = Field(default=None)


class KnowledgeIndexJob(BaseModel, table=True):
    """Durable idempotent indexing task claimed by a worker."""

    __tablename__ = "knowledge_index_jobs"

    file_id: UUID = Field(foreign_key="files.id", index=True)
    user_id: UUID | None = Field(default=None, index=True)
    index_version: str = Field(default="rag-v1", max_length=64, index=True)
    content_hash: str | None = Field(default=None, max_length=64)
    status: KnowledgeIndexJobStatus = Field(
        default=KnowledgeIndexJobStatus.QUEUED,
        sa_column=enum_value_column(KnowledgeIndexJobStatus, index=True),
    )
    available_at: datetime = Field(index=True)
    locked_at: datetime | None = Field(default=None)
    locked_by: str | None = Field(default=None, max_length=128)
    attempt_count: int = Field(default=0)
    last_error: str | None = Field(default=None, sa_column=Column(Text))


class KnowledgeIndexStateRead(SQLModel):
    """Stable API/read shape for index state."""

    file_id: UUID
    index_version: str
    content_hash: str | None
    status: KnowledgeIndexStatus
    chunk_count: int
    embedded_count: int
    attempt_count: int
    last_error: str | None
    started_at: datetime | None
    completed_at: datetime | None
