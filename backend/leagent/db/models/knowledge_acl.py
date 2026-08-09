"""Organization, knowledge-space, and document ACL models."""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID  # noqa: TC003 - SQLModel resolves UUID fields at runtime

from sqlmodel import Field, SQLModel

from leagent.db.models.base import BaseModel, enum_value_column


class WorkspaceMemberRole(StrEnum):
    """Organization-level membership role."""

    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"


class KnowledgeSpaceMemberRole(StrEnum):
    """Knowledge-space membership role."""

    MANAGER = "manager"
    EDITOR = "editor"
    VIEWER = "viewer"


class KnowledgeAccessScope(StrEnum):
    """Visibility policy attached to a knowledge document."""

    PRIVATE = "private"
    SPACE = "space"
    RESTRICTED = "restricted"


class WorkspaceMember(BaseModel, table=True):
    """User membership in an organization/workspace."""

    __tablename__ = "workspace_members"

    workspace_id: UUID = Field(foreign_key="workspaces.id", index=True)
    user_id: UUID = Field(foreign_key="users.id", index=True)
    role: WorkspaceMemberRole = Field(
        default=WorkspaceMemberRole.MEMBER,
        sa_column=enum_value_column(WorkspaceMemberRole, index=True),
    )


class KnowledgeSpace(BaseModel, table=True):
    """A named, independently permissioned knowledge collection."""

    __tablename__ = "knowledge_spaces"

    workspace_id: UUID = Field(foreign_key="workspaces.id", index=True)
    name: str = Field(max_length=160)
    slug: str = Field(max_length=160, index=True)
    description: str | None = Field(default=None, max_length=2000)
    default_access_scope: KnowledgeAccessScope = Field(
        default=KnowledgeAccessScope.SPACE,
        sa_column=enum_value_column(KnowledgeAccessScope, index=True),
    )
    is_default: bool = Field(default=False, index=True)
    is_deleted: bool = Field(default=False, index=True)


class KnowledgeSpaceMember(BaseModel, table=True):
    """User membership in a knowledge space."""

    __tablename__ = "knowledge_space_members"

    space_id: UUID = Field(foreign_key="knowledge_spaces.id", index=True)
    user_id: UUID = Field(foreign_key="users.id", index=True)
    role: KnowledgeSpaceMemberRole = Field(
        default=KnowledgeSpaceMemberRole.VIEWER,
        sa_column=enum_value_column(KnowledgeSpaceMemberRole, index=True),
    )


class KnowledgeDocumentGrant(BaseModel, table=True):
    """Explicit read/write grant for a restricted document."""

    __tablename__ = "knowledge_document_grants"

    file_id: UUID = Field(foreign_key="files.id", index=True)
    user_id: UUID = Field(foreign_key="users.id", index=True)
    permission: str = Field(default="read", max_length=20, index=True)


class KnowledgeSpaceCreate(SQLModel):
    """Create payload for a knowledge space."""

    name: str = Field(min_length=1, max_length=160)
    slug: str | None = Field(default=None, max_length=160)
    description: str | None = Field(default=None, max_length=2000)
    default_access_scope: KnowledgeAccessScope = KnowledgeAccessScope.SPACE


class KnowledgeSpaceRead(SQLModel):
    """Read payload for a knowledge space."""

    id: UUID
    workspace_id: UUID
    name: str
    slug: str
    description: str | None
    default_access_scope: KnowledgeAccessScope
    is_default: bool


class KnowledgeSpaceMemberRead(SQLModel):
    """Read payload for a space membership."""

    user_id: UUID
    role: KnowledgeSpaceMemberRole


__all__ = [
    "KnowledgeAccessScope",
    "KnowledgeDocumentGrant",
    "KnowledgeSpace",
    "KnowledgeSpaceCreate",
    "KnowledgeSpaceMember",
    "KnowledgeSpaceMemberRead",
    "KnowledgeSpaceMemberRole",
    "KnowledgeSpaceRead",
    "WorkspaceMember",
    "WorkspaceMemberRole",
]
