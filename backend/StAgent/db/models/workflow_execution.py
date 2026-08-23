"""SQLModel database models for workflow executions."""

from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import UUID

from sqlmodel import Column, Field, SQLModel, Text

from StAgent.db.models.base import UUIDMixin


class WorkflowExecution(UUIDMixin, SQLModel, table=True):
    """Persistent record of a workflow execution run."""

    __tablename__ = "workflow_executions"

    flow_id: UUID | None = Field(default=None, foreign_key="flows.id", index=True)
    user_id: UUID | None = Field(default=None, foreign_key="users.id", index=True)
    cron_job_id: UUID | None = Field(default=None, index=True)
    workspace_id: UUID | None = Field(default=None, nullable=True, index=True)

    prompt_id: str | None = Field(default=None, max_length=100, index=True)
    graph_hash: str | None = Field(default=None, max_length=128)
    priority: int = Field(default=5)

    status: str = Field(default="pending", max_length=50, index=True)
    trigger_type: str = Field(default="manual", max_length=50)

    inputs: str | None = Field(default=None, sa_column=Column(Text))
    outputs: str | None = Field(default=None, sa_column=Column(Text))
    execution_history: str | None = Field(default=None, sa_column=Column(Text))

    current_node: str | None = Field(default=None, max_length=200)
    node_count: int = Field(default=0)
    error: str | None = Field(default=None, sa_column=Column(Text))
    error_stack: str | None = Field(default=None, sa_column=Column(Text))

    started_at: datetime | None = Field(default=None)
    completed_at: datetime | None = Field(default=None)
    duration_ms: int = Field(default=0)

    workflow_state_id: UUID | None = Field(default=None)
    retry_count: int = Field(default=0)

    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class WorkflowExecutionRead(SQLModel):
    """Read schema for workflow executions."""

    id: UUID
    flow_id: UUID | None
    user_id: UUID | None
    cron_job_id: UUID | None
    prompt_id: str | None
    graph_hash: str | None
    priority: int
    status: str
    trigger_type: str
    inputs: str | None
    outputs: str | None
    current_node: str | None
    node_count: int
    error: str | None
    started_at: datetime | None
    completed_at: datetime | None
    duration_ms: int
    created_at: datetime
    updated_at: datetime
