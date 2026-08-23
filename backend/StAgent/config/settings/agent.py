"""Agent settings sub-module."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict

from StAgent.config.settings._types import RuntimeProfile


class AgentSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AGENT_")

    runtime_profile: RuntimeProfile = "standard"
    max_iterations: int = 15
    default_timeout_sec: int = 300
    max_tool_calls_per_turn: int = 10
    browser_pool_size: int = 5
    celery_concurrency: int = 4
    context_window_tokens: int = 128_000
    max_output_tokens: int = 8192

    conversation_timeout_sec: int = 600
    max_executions_per_session: int = 5
    stream_drain_timeout_sec: int = 300
    stream_queue_maxsize: int = 512
    long_task_timeout_sec: int = 3600
    long_stream_drain_timeout_sec: int = 3600
    long_max_turns: int = 60
    long_max_tool_calls_per_turn: int = 20
    long_tool_timeout_sec: int = 1800
    long_max_concurrency_per_user: int = 1
    long_max_concurrency_per_workspace: int = 3
    extended_task_timeout_sec: int = 7200
    extended_stream_drain_timeout_sec: int = 7200
    extended_max_turns: int = 120
    extended_max_tool_calls_per_turn: int = 30
    extended_tool_timeout_sec: int = 3600
    extended_max_concurrency_per_user: int = 1
    extended_max_concurrency_per_workspace: int = 2
