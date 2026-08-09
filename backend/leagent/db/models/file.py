"""File model for file management."""

from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Optional
from uuid import UUID

from sqlmodel import Column, Field, Relationship, SQLModel, Text

from leagent.db.models.base import BaseModel, SoftDeleteMixin, enum_value_column
from leagent.db.models.knowledge_acl import KnowledgeAccessScope

if TYPE_CHECKING:
    from leagent.db.models.folder import Folder


class FileType(str, Enum):
    """File type classification."""

    DOCUMENT = "document"
    IMAGE = "image"
    AUDIO = "audio"
    VIDEO = "video"
    ARCHIVE = "archive"
    DATA = "data"
    CODE = "code"
    OTHER = "other"


class FileStatus(str, Enum):
    """File processing status."""

    UPLOADED = "uploaded"
    PROCESSING = "processing"
    PROCESSED = "processed"
    FAILED = "failed"


class LibraryScope(str, Enum):
    """Logical catalog scope — identity by column, not storage path."""

    WORKSPACE = "workspace"
    KNOWLEDGE = "knowledge"
    ARTIFACT = "artifact"


class InboxState(str, Enum):
    """Information-hub triage state for newly ingested files."""

    NEW = "new"
    TRIAGED = "triaged"
    DISMISSED = "dismissed"


class FileBase(SQLModel):
    """Base file fields."""

    name: str = Field(max_length=255)
    original_name: str = Field(max_length=255)
    file_type: FileType = Field(
        default=FileType.OTHER,
        sa_column=enum_value_column(FileType),
    )
    mime_type: str | None = Field(default=None, max_length=100)
    size: int = Field(default=0)
    status: FileStatus = Field(
        default=FileStatus.UPLOADED,
        sa_column=enum_value_column(FileStatus),
    )


class File(FileBase, BaseModel, SoftDeleteMixin, table=True):
    """File database model."""

    __tablename__ = "files"

    # Ownership
    user_id: UUID | None = Field(default=None, foreign_key="users.id", index=True)
    workspace_id: UUID | None = Field(default=None, nullable=True, index=True)
    knowledge_space_id: UUID | None = Field(
        default=None,
        foreign_key="knowledge_spaces.id",
        nullable=True,
        index=True,
    )
    access_scope: KnowledgeAccessScope = Field(
        default=KnowledgeAccessScope.PRIVATE,
        sa_column=enum_value_column(
            KnowledgeAccessScope,
            nullable=True,
            index=True,
        ),
    )
    folder_id: UUID | None = Field(default=None, foreign_key="folders.id", index=True)
    # Chat attachments use a non-null ``session_id``. Pet Space library files
    # (and other non-chat assets) use ``session_id is NULL`` and are linked
    # via :class:`PetProjectFile` instead of the session attachment list.
    session_id: UUID | None = Field(
        default=None, foreign_key="chat_sessions.id", index=True, nullable=True
    )

    # Storage
    storage_path: str = Field(max_length=1000)
    storage_bucket: str | None = Field(default=None, max_length=100)
    checksum: str | None = Field(default=None, max_length=64)

    # Processing results
    extracted_text: str | None = Field(default=None, sa_column=Column(Text))
    file_metadata: str | None = Field(default=None, sa_column=Column(Text))  # JSON
    # Extractive blurb for knowledge catalog (set after chunk indexing)
    summary: str | None = Field(default=None, sa_column=Column(Text))

    # OCR/extraction info
    page_count: int | None = Field(default=None)
    has_ocr: bool = Field(default=False)
    ocr_language: str | None = Field(default=None, max_length=20)

    # Vector embedding reference
    embedding_id: str | None = Field(default=None, max_length=100)
    is_indexed: bool = Field(default=False)

    # Expiration
    expires_at: datetime | None = Field(default=None)

    # Unified library catalog (see migration 0005_library_layer)
    library_scope: LibraryScope = Field(
        default=LibraryScope.WORKSPACE,
        sa_column=enum_value_column(LibraryScope, index=True),
    )
    inbox_state: InboxState = Field(
        default=InboxState.NEW,
        sa_column=enum_value_column(InboxState, index=True),
    )
    origin_type: str | None = Field(default=None, max_length=40, index=True)
    origin_ref: str | None = Field(default=None, max_length=100, index=True)
    is_pinned: bool = Field(default=False)

    # Relationships
    folder: Optional["Folder"] = Relationship(back_populates="files")


class FileCreate(SQLModel):
    """Schema for creating a file."""

    name: str
    original_name: str
    file_type: FileType = FileType.OTHER
    mime_type: str | None = None
    size: int
    storage_path: str
    storage_bucket: str | None = None
    folder_id: UUID | None = None
    workspace_id: UUID | None = None
    knowledge_space_id: UUID | None = None
    access_scope: KnowledgeAccessScope | None = None


class FileUpdate(SQLModel):
    """Schema for updating a file."""

    name: str | None = None
    status: FileStatus | None = None
    folder_id: UUID | None = None
    extracted_text: str | None = None
    file_metadata: str | None = None
    summary: str | None = None
    is_indexed: bool | None = None
    library_scope: LibraryScope | None = None
    inbox_state: InboxState | None = None
    is_pinned: bool | None = None
    workspace_id: UUID | None = None
    knowledge_space_id: UUID | None = None
    access_scope: KnowledgeAccessScope | None = None


class FileRead(FileBase):
    """Schema for reading a file."""

    id: UUID
    user_id: UUID | None
    workspace_id: UUID | None = None
    knowledge_space_id: UUID | None = None
    access_scope: KnowledgeAccessScope | None = None
    folder_id: UUID | None
    checksum: str | None
    page_count: int | None
    has_ocr: bool
    is_indexed: bool
    summary: str | None = None
    library_scope: LibraryScope = LibraryScope.WORKSPACE
    inbox_state: InboxState = InboxState.TRIAGED
    origin_type: str | None = None
    origin_ref: str | None = None
    is_pinned: bool = False
    created_at: datetime
    updated_at: datetime
