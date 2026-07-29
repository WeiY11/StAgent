"""Customer complaint ticket model."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlmodel import Column, Field, SQLModel, Text

from leagent.db.models.base import BaseModel, SoftDeleteMixin

# Status / category / severity / source values are stored as plain strings
# (same convention as ``CronJobModel.status``). Keep these tuples in sync with
# the API layer's Literal types.
COMPLAINT_STATUSES: tuple[str, ...] = ("open", "in_progress", "resolved", "closed")
COMPLAINT_CATEGORIES: tuple[str, ...] = (
    "product",
    "service",
    "billing",
    "logistics",
    "other",
)
COMPLAINT_SEVERITIES: tuple[str, ...] = ("low", "medium", "high", "urgent")
COMPLAINT_SOURCES: tuple[str, ...] = ("manual", "chat")


class ComplaintBase(SQLModel):
    """Base complaint fields."""

    title: str = Field(index=True, max_length=200)
    description: str | None = Field(default=None, sa_column=Column(Text))
    customer_name: str | None = Field(default=None, max_length=200)
    customer_contact: str | None = Field(default=None, max_length=200)
    category: str = Field(default="other", max_length=50)
    severity: str = Field(default="medium", max_length=50)


class Complaint(ComplaintBase, BaseModel, SoftDeleteMixin, table=True):
    """Complaint ticket database model."""

    __tablename__ = "complaints"

    status: str = Field(default="open", max_length=50, index=True)
    source: str = Field(default="manual", max_length=50)
    session_id: str | None = Field(default=None, max_length=100)

    resolution: str | None = Field(default=None, sa_column=Column(Text))
    resolved_at: datetime | None = Field(default=None)

    user_id: UUID | None = Field(default=None, foreign_key="users.id", index=True)
    workspace_id: UUID | None = Field(default=None, nullable=True, index=True)


class ComplaintCreate(ComplaintBase):
    """Schema for creating a complaint."""

    source: str = "manual"
    session_id: str | None = None


class ComplaintUpdate(SQLModel):
    """Schema for updating a complaint."""

    title: str | None = None
    description: str | None = None
    customer_name: str | None = None
    customer_contact: str | None = None
    category: str | None = None
    severity: str | None = None
    status: str | None = None
    resolution: str | None = None


class ComplaintRead(ComplaintBase):
    """Schema for reading a complaint."""

    id: UUID
    status: str
    source: str
    session_id: str | None
    resolution: str | None
    resolved_at: datetime | None
    user_id: UUID | None
    created_at: datetime
    updated_at: datetime
