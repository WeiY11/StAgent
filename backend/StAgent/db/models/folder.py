"""Folder model for organizing files and flows."""

from datetime import datetime
from typing import TYPE_CHECKING, List, Optional
from uuid import UUID

from sqlmodel import Field, Relationship, SQLModel

from StAgent.db.models.base import BaseModel, SoftDeleteMixin

if TYPE_CHECKING:
    from StAgent.db.models.file import File
    from StAgent.db.models.flow import Flow


class FolderBase(SQLModel):
    """Base folder fields."""

    name: str = Field(max_length=200)
    description: str | None = Field(default=None, max_length=500)
    icon: str | None = Field(default="📁", max_length=50)
    color: str | None = Field(default=None, max_length=20)


class Folder(FolderBase, BaseModel, SoftDeleteMixin, table=True):
    """Folder database model."""

    __tablename__ = "folders"

    # Ownership
    user_id: UUID | None = Field(default=None, foreign_key="users.id", index=True)
    workspace_id: UUID | None = Field(default=None, nullable=True, index=True)

    # Hierarchy
    parent_id: UUID | None = Field(default=None, foreign_key="folders.id", index=True)

    # Ordering
    position: int = Field(default=0)

    # Counts (cached for performance)
    file_count: int = Field(default=0)
    flow_count: int = Field(default=0)

    # Code-project mode. When ``is_project`` is True the folder is bound to
    # an on-disk directory (``project_path``) that the coding agent /
    # project_* tools operate on. When False the folder is an ordinary DB
    # association of uploaded files, exactly as before.
    is_project: bool = Field(default=False, index=True)
    project_path: str | None = Field(default=None, max_length=1024)
    project_path_checked_at: datetime | None = Field(default=None)

    # Relationships
    files: list["File"] = Relationship(back_populates="folder")
    flows: list["Flow"] = Relationship(back_populates="folder")


class FolderCreate(SQLModel):
    """Schema for creating a folder."""

    name: str
    description: str | None = None
    icon: str | None = "📁"
    color: str | None = None
    parent_id: UUID | None = None


class FolderUpdate(SQLModel):
    """Schema for updating a folder."""

    name: str | None = None
    description: str | None = None
    icon: str | None = None
    color: str | None = None
    parent_id: UUID | None = None
    position: int | None = None


class FolderProjectUpdate(SQLModel):
    """Schema for toggling / configuring a folder's code-project mode."""

    enabled: bool
    project_path: str | None = None


class FolderRead(FolderBase):
    """Schema for reading a folder."""

    id: UUID
    user_id: UUID | None
    parent_id: UUID | None
    position: int
    file_count: int
    flow_count: int
    is_project: bool = False
    project_path: str | None = None
    project_path_checked_at: datetime | None = None
