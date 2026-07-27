"""Chat project model for grouping protected conversations."""

from __future__ import annotations

from typing import Optional
from uuid import UUID

from sqlmodel import Column, Field, Text

from leagent.db.models.base import BaseModel, SoftDeleteMixin


class ChatProject(BaseModel, SoftDeleteMixin, table=True):
    """Designer-facing project workspace for related chat sessions."""

    __tablename__ = "chat_projects"

    user_id: UUID = Field(foreign_key="users.id", index=True)
    workspace_id: UUID | None = Field(default=None, foreign_key="workspaces.id", index=True)
    #: Linked catalog Folder under 「文件」— shared by all sessions in this project.
    folder_id: UUID | None = Field(
        default=None,
        foreign_key="folders.id",
        index=True,
        unique=True,
    )
    name: str = Field(max_length=200)
    description: str | None = Field(default=None, sa_column=Column(Text))
    design_context: str | None = Field(default=None, sa_column=Column(Text))
    settings: str | None = Field(default=None, sa_column=Column(Text))
    password_hash: str | None = Field(default=None, max_length=512)
