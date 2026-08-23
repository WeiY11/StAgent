"""Durable agent-run debug/eval traces (separate from checkpoints & chat SSOT)."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Column, Text
from sqlmodel import Field

from StAgent.db.models.base import BaseModel, utc_now


class AgentTrace(BaseModel, table=True):
    """One row per :class:`~StAgent.runtime.execution_run.ExecutionRun`."""

    __tablename__ = "agent_traces"

    trace_id: str = Field(index=True, unique=True, max_length=64)
    parent_trace_id: str | None = Field(default=None, index=True, max_length=64)
    session_id: str | None = Field(default=None, index=True, max_length=100)
    user_id: str | None = Field(default=None, index=True, max_length=100)
    scope: str = Field(default="chat_turn", index=True, max_length=32)
    agent_name: str = Field(default="", max_length=200)
    model: str = Field(default="", index=True, max_length=200)
    status: str = Field(default="running", index=True, max_length=32)
    terminal_reason: str | None = Field(default=None, max_length=64)
    started_at: datetime = Field(default_factory=utc_now)
    ended_at: datetime | None = Field(default=None)
    latency_ms: float = Field(default=0.0)
    input_tokens: int = Field(default=0)
    output_tokens: int = Field(default=0)
    cache_read_tokens: int = Field(default=0)
    cache_miss_tokens: int = Field(default=0)
    total_cost_usd: float = Field(default=0.0)
    tool_call_count: int = Field(default=0)
    llm_call_count: int = Field(default=0)
    experiment_id: str | None = Field(default=None, index=True, max_length=64)
    prompt_hash: str | None = Field(default=None, max_length=64)
    tags: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    error: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    scores: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    root_span_id: str | None = Field(default=None, max_length=64)


class AgentTraceSpan(BaseModel, table=True):
    """Append-only span within an agent trace."""

    __tablename__ = "agent_trace_spans"

    span_id: str = Field(index=True, max_length=64)
    parent_span_id: str | None = Field(default=None, index=True, max_length=64)
    trace_id: str = Field(index=True, max_length=64)
    seq: int = Field(default=0, index=True)
    kind: str = Field(default="event", index=True, max_length=32)
    name: str = Field(default="", max_length=300)
    status: str = Field(default="ok", max_length=32)
    started_at: datetime = Field(default_factory=utc_now)
    ended_at: datetime | None = Field(default=None)
    latency_ms: float = Field(default=0.0)
    attrs: str | None = Field(default=None, sa_column=Column(Text))
    input_preview: str | None = Field(default=None, sa_column=Column(Text))
    output_preview: str | None = Field(default=None, sa_column=Column(Text))
    payload_ref: str | None = Field(default=None, max_length=500)


class AgentTraceExperiment(BaseModel, table=True):
    """Same-prompt multi-model comparison experiment."""

    __tablename__ = "agent_trace_experiments"

    experiment_id: str = Field(index=True, unique=True, max_length=64)
    name: str = Field(default="", max_length=200)
    prompt: str = Field(default="", sa_column=Column(Text))
    session_id: str | None = Field(default=None, index=True, max_length=100)
    model_ids: str = Field(default="[]", sa_column=Column(Text))
    created_by: str | None = Field(default=None, max_length=100)
    status: str = Field(default="pending", index=True, max_length=32)
    error: str | None = Field(default=None, sa_column=Column(Text))


__all__ = ["AgentTrace", "AgentTraceSpan", "AgentTraceExperiment"]
