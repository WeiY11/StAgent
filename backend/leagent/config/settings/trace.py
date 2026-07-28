"""Trace settings sub-module."""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class TraceSettings(BaseSettings):
    """Durable agent running-trace (debug/eval) plane.

    Separate from ``CheckpointStore`` (resume) and chat transcript SSOT.
    Env prefix: ``LEAGENT_TRACE_*``.
    """

    model_config = SettingsConfigDict(env_prefix="LEAGENT_TRACE_")

    enabled: bool = True
    capture_payloads: bool = False
    #: Persist span input/output preview text. Off by default — metadata,
    #: timings, token counts, and tool names are enough for session traces.
    record_previews: bool = False
    preview_chars: int = Field(default=512, ge=64, le=64_000)
    #: Coalesce pending writes for this many ms (0 = flush immediately).
    flush_interval_ms: int = Field(default=50, ge=0, le=5_000)
    retention_days: int = Field(default=30, ge=1, le=3650)
