"""Application services settings sub-modules.

Contains: CanvasSettings, RtspStreamSettings, CodingProjectsSettings,
SessionSettings, PromptSettings, ContextSettings, WorkflowEngineSettings,
CacheSettings.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from leagent.config.settings._types import CANVAS_PREVIEW_CSP_LOCAL


class CanvasSettings(BaseSettings):
    """Agent canvas preview settings (local-mode defaults)."""

    model_config = SettingsConfigDict(env_prefix="CANVAS_")

    preview_public_base: str = ""
    preview_signing_secret: str = ""
    preview_token_ttl_seconds: int = 60 * 45
    max_html_bytes: int = 4 * 1024 * 1024
    max_ui_snapshot_bytes: int = 512 * 1024
    max_tree_depth: int = 96
    max_nodes_per_tree: int = 2000
    google_maps_api_key: str = ""
    preview_csp: str = CANVAS_PREVIEW_CSP_LOCAL
    embed_allow_loopback: bool = True


class RtspStreamSettings(BaseSettings):
    """RTSP -> browser preview via server-side ffmpeg (MJPEG over HTTP)."""

    model_config = SettingsConfigDict(env_prefix="RTSP_STREAM_")

    enabled: bool = False
    ffmpeg_path: str = "ffmpeg"
    token_ttl_seconds: int = 15 * 60
    max_url_chars: int = 2048
    scale_max_width: int = 1280
    jpeg_quality: int = 7


class CodingProjectsSettings(BaseSettings):
    """Configuration for the coding-project live-preview service."""

    model_config = SettingsConfigDict(env_prefix="CODING_PROJECTS_")

    enabled: bool = True
    root: str = ""
    bind_host: str = "127.0.0.1"
    port_range_min: int = 39000
    port_range_max: int = 39999
    max_concurrent_per_user: int = 3
    idle_ttl_sec: int = 1800
    npm_install_timeout_sec: int = 600
    dev_server_startup_timeout_sec: int = 120
    log_buffer_lines: int = 4000
    allowed_binaries: str = "node,npm,pnpm,yarn,python,python3,uvicorn"
    preview_token_ttl_seconds: int = 60 * 30
    use_docker: bool = False


class SessionSettings(BaseSettings):
    """Configuration for SessionManager + session store."""

    model_config = SettingsConfigDict(env_prefix="SESSION_")

    in_memory_lru_size: int = 256
    max_messages_per_session: int = 2000
    autocompact_token_threshold: int = 96_000
    autocompact_keep_recent: int = 20
    inactive_session_ttl_days: int = 90
    workspace_file_ttl_hours: int = 168
    object_store_ttl_days: int = 30


class PromptSettings(BaseSettings):
    """Configuration for the leagent.prompts package."""

    model_config = SettingsConfigDict(env_prefix="LEAGENT_PROMPT_")

    templates_dir: str = ""
    hot_reload: bool = False
    max_total_chars: int = 24_000
    per_layer_budget_chars: dict[str, int] = Field(default_factory=dict)
    enable_cache_boundaries: bool = True


class ContextSettings(BaseSettings):
    """Configuration for the context management system."""

    model_config = SettingsConfigDict(env_prefix="LEAGENT_CONTEXT_")

    project_memory_denylist: list[str] = Field(
        default_factory=lambda: ["**/leagent/AGENTS.md", "**/backend/AGENTS.md"],
    )
    project_memory_allowlist: list[str] = Field(default_factory=list)
    respect_git_boundary: bool = True

    file_state_max_entries: int = 64
    file_state_max_tokens: int = 16_000

    working_set_excerpt_head_lines: int = 20
    working_set_excerpt_tail_lines: int = 10

    recall_attachment_limit: int = 5
    tool_history_attachment_limit: int = 5
    recent_reads_attachment_limit: int = 5

    budget_max_chars: int = 24_000
    freshness_half_life_seconds: float = 300.0


class WorkflowEngineSettings(BaseSettings):
    """Settings for the workflow engine (in-memory queue only)."""

    model_config = SettingsConfigDict(env_prefix="LEAGENT_WORKFLOW_")

    queue_backend: Literal["memory"] = "memory"
    cache_mode: Literal["lru", "ram", "none"] = "lru"
    cache_lru_size: int = 128
    cache_ram_mb: int = 256
    worker_concurrency: int = 1
    custom_nodes_dir: str = ""
    hot_reload: bool = False
    event_bus_prefix: str = "workflow:events"


class CacheSettings(BaseSettings):
    """In-memory :class:`CacheService` tuning knobs.

    ``namespace_ttls`` overrides the default TTL per logical namespace so hot,
    cheap-to-recompute domains (e.g. ``recall``) expire fast while expensive,
    stable ones (e.g. ``embeddings``) live long — this is the primary lever for
    raising the aggregate hit rate. Env prefix: ``LEAGENT_CACHE_*``; the map
    accepts JSON, e.g. ``LEAGENT_CACHE_NAMESPACE_TTLS='{"embeddings":7200}'``.
    """

    model_config = SettingsConfigDict(env_prefix="LEAGENT_CACHE_")

    default_ttl: int = Field(default=3600, ge=1)
    max_items: int = Field(default=10_000, ge=1)
    max_value_bytes: int = Field(default=1_048_576, ge=1_024)
    maintenance_interval_sec: float = Field(default=30.0, ge=0.0, le=3_600.0)
    ttl_jitter_ratio: float = Field(default=0.05, ge=0.0, le=0.25)
    shard_count: int = Field(default=1, ge=1, le=64)
    tool_cache_max_items: int = Field(default=5_000, ge=1)
    memory_l1_max_items: int = Field(default=512, ge=1)
    memory_embedding_l1_max_items: int = Field(default=1_024, ge=1)
    memory_recall_l1_max_items: int = Field(default=256, ge=1)
    namespace_ttls: dict[str, int] = Field(
        default_factory=lambda: {
            "embeddings": 7200,
            "recall": 300,
            "files": 1800,
            "chat": 900,
            "memory": 1800,
            "workflow_node_output": 900,
            "webhooks": 600,
            "variables": 1800,
        }
    )
    warm_on_start: bool = False

    @model_validator(mode="after")
    def validate_cache_capacity(self) -> CacheSettings:
        if self.max_items < self.shard_count:
            raise ValueError("cache max_items must be >= shard_count")
        return self

    def ttl_for(self, namespace: str | None) -> int:
        """Resolve the effective TTL for *namespace* (falls back to default)."""
        if namespace and namespace in self.namespace_ttls:
            return int(self.namespace_ttls[namespace])
        return int(self.default_ttl)


class KnowledgeRetrievalSettings(BaseSettings):
    """Quality-oriented RAG backends and policy knobs.

    Empty endpoints keep the zero-dependency SQLite/BM25 path.  Production
    deployments can enable OpenSearch/Milvus and a cross-encoder reranker
    without changing the chat or tool contracts.
    """

    model_config = SettingsConfigDict(env_prefix="LEAGENT_KNOWLEDGE_")

    index_version: str = "rag-v1"
    opensearch_url: str = ""
    opensearch_index: str = "knowledge_chunks"
    opensearch_api_key: str = ""
    opensearch_timeout_seconds: float = Field(default=5.0, ge=0.1, le=60.0)
    milvus_enabled: bool = False
    milvus_host: str = "localhost"
    milvus_port: int = Field(default=19530, ge=1, le=65535)
    milvus_uri: str = ""
    milvus_token: str = ""
    rerank_endpoint: str = ""
    rerank_api_key: str = ""
    rerank_timeout_seconds: float = Field(default=5.0, ge=0.1, le=60.0)
    query_rewrite_enabled: bool = False
    strict_evidence: bool = True
    max_context_tokens: int = Field(default=6000, ge=512, le=32000)

    # Offline retrieval-evaluation gates.  These values are deliberately
    # configuration, not hard-coded assertions, so a deployment can stage a
    # new index version and compare it against the same acceptance contract.
    recall_at_10_target: float = Field(default=0.90, ge=0.0, le=1.0)
    ndcg_at_10_target: float = Field(default=0.80, ge=0.0, le=1.0)
    citation_accuracy_target: float = Field(default=0.95, ge=0.0, le=1.0)
    hallucination_rate_max: float = Field(default=0.02, ge=0.0, le=1.0)
    retrieval_p95_ms_target: float = Field(default=5000.0, ge=1.0, le=120_000.0)
