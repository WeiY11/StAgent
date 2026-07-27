"""Flow model for workflow/agent definitions."""

from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Optional
from uuid import UUID

from sqlmodel import Column, Field, Relationship, SQLModel, Text

from leagent.db.models.base import BaseModel, SoftDeleteMixin, enum_value_column

if TYPE_CHECKING:
    from leagent.db.models.folder import Folder
    from leagent.db.models.message import Message


class FlowStatus(str, Enum):
    """Flow status values."""

    DRAFT = "draft"
    PUBLISHED = "published"
    ARCHIVED = "archived"


class FlowType(str, Enum):
    """Flow type classification."""

    AGENT = "agent"
    WORKFLOW = "workflow"
    CHAT = "chat"
    TOOL = "tool"


class FlowBase(SQLModel):
    """Base flow fields."""

    name: str = Field(index=True, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    icon: str | None = Field(default="🤖", max_length=50)
    icon_bg_color: str | None = Field(default=None, max_length=20)
    status: FlowStatus = Field(
        default=FlowStatus.DRAFT,
        sa_column=enum_value_column(FlowStatus),
    )
    flow_type: FlowType = Field(
        default=FlowType.AGENT,
        sa_column=enum_value_column(FlowType),
    )
    is_public: bool = Field(default=False)
    tags: str | None = Field(default=None, max_length=500)


class Flow(FlowBase, BaseModel, SoftDeleteMixin, table=True):
    """Flow database model."""

    __tablename__ = "flows"

    # Flow definition (JSON)
    data: str | None = Field(default=None, sa_column=Column(Text))

    # Versioning
    version: int = Field(default=1)
    parent_id: UUID | None = Field(default=None, foreign_key="flows.id")

    # Ownership
    user_id: UUID | None = Field(default=None, foreign_key="users.id")
    folder_id: UUID | None = Field(default=None, foreign_key="folders.id")
    # Tenant scoping (nullable → belongs to user's personal workspace).
    workspace_id: UUID | None = Field(default=None, nullable=True, index=True)

    # Execution settings (JSON)
    settings: str | None = Field(default=None, sa_column=Column(Text))

    # Stats
    run_count: int = Field(default=0)
    last_run_at: datetime | None = Field(default=None)
    avg_run_time_ms: int | None = Field(default=None)

    # Embedding endpoint for RAG
    endpoint_name: str | None = Field(default=None, max_length=100)

    # Relationships
    folder: Optional["Folder"] = Relationship(back_populates="flows")
    messages: list["Message"] = Relationship(back_populates="flow")


class FlowVersion(BaseModel, table=True):
    """Versioned flow snapshots."""

    __tablename__ = "flow_versions"

    flow_id: UUID = Field(foreign_key="flows.id", index=True)
    version: int = Field(index=True)
    name: str = Field(max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    data: str | None = Field(default=None, sa_column=Column(Text))
    settings: str | None = Field(default=None, sa_column=Column(Text))
    created_by: UUID | None = Field(default=None, foreign_key="users.id")
    change_log: str | None = Field(default=None, max_length=1000)
    workspace_id: UUID | None = Field(default=None, nullable=True, index=True)


class FlowCreate(FlowBase):
    """Schema for creating a flow."""

    data: str | None = None
    settings: str | None = None
    folder_id: UUID | None = None


class FlowUpdate(SQLModel):
    """Schema for updating a flow."""

    name: str | None = None
    description: str | None = None
    icon: str | None = None
    icon_bg_color: str | None = None
    status: FlowStatus | None = None
    flow_type: FlowType | None = None
    is_public: bool | None = None
    tags: str | None = None
    data: str | None = None
    settings: str | None = None
    folder_id: UUID | None = None


class FlowRead(FlowBase):
    """Schema for reading a flow."""

    id: UUID
    version: int
    user_id: UUID | None
    folder_id: UUID | None
    run_count: int
    last_run_at: datetime | None
    created_at: datetime
    updated_at: datetime
    # JSON graph: either `{ "nodes", "edges" }` (editor) or engine workflow dict from templates
    data: str | None = None
    settings: str | None = None
