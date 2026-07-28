"""Pydantic-based application settings with nested configuration groups.

Standalone single-node configuration — SQLite-only, no external services.

This package splits the monolithic settings into domain sub-modules while
maintaining full backward compatibility. All names that were previously
importable from ``leagent.config.settings`` continue to work.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

import leagent.config.env_bootstrap  # noqa: F401 — load ~/.leagent/.env early

# --- Sub-module imports (re-exported for backward compatibility) -----------
from leagent.config.settings._types import (
    CANVAS_PREVIEW_CSP_LOCAL,
    LogFormat,
    RuntimeProfile,
)
from leagent.config.settings.agent import AgentSettings
from leagent.config.settings.database import DatabaseSettings
from leagent.config.settings.files import (
    FilesSettings,
    _default_task_output_dir,
    _default_upload_dir,
)
from leagent.config.settings.llm import LLMSettings
from leagent.config.settings.security import SecuritySettings
from leagent.config.settings.services import (
    CacheSettings,
    CanvasSettings,
    CodingProjectsSettings,
    ContextSettings,
    PromptSettings,
    RtspStreamSettings,
    SessionSettings,
    WorkflowEngineSettings,
)
from leagent.config.settings.trace import TraceSettings
from leagent.config.settings.web import (
    ImageSearchSettings,
    WebBrowserSettings,
    WebFetchSettings,
    WebSearchSettings,
)

# ---------------------------------------------------------------------------
# Root Settings class
# ---------------------------------------------------------------------------


class Settings(BaseSettings):
    """Root settings for standalone single-node deployment."""

    model_config = SettingsConfigDict(
        env_prefix="LEAGENT_",
        env_nested_delimiter="__",
        case_sensitive=False,
    )

    app_name: str = "LeAgent"
    version: str = "1.2.0"
    environment: Literal["development", "staging", "production"] = "development"
    debug: bool = True
    log_level: str = "INFO"
    log_format: LogFormat = "auto"
    log_file: str = ""
    host: str = "0.0.0.0"
    port: int = 7860
    workers: int = 1
    skip_startup_migrations: bool = False
    #: HMAC signing secret for session JWTs / signed URLs (also ``LEAGENT_SECRET_KEY``).
    secret_key: str = Field(
        default="",
        validation_alias=AliasChoices("LEAGENT_SECRET_KEY", "SECRET_KEY"),
    )

    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    agent: AgentSettings = Field(default_factory=AgentSettings)
    files: FilesSettings = Field(default_factory=FilesSettings)
    session: SessionSettings = Field(default_factory=SessionSettings)
    canvas: CanvasSettings = Field(default_factory=CanvasSettings)
    image_search: ImageSearchSettings = Field(default_factory=ImageSearchSettings)
    web_browser: WebBrowserSettings = Field(default_factory=WebBrowserSettings)
    web_search: WebSearchSettings = Field(default_factory=WebSearchSettings)
    web_fetch: WebFetchSettings = Field(default_factory=WebFetchSettings)
    rtsp_stream: RtspStreamSettings = Field(default_factory=RtspStreamSettings)
    coding_projects: CodingProjectsSettings = Field(default_factory=CodingProjectsSettings)
    prompt: PromptSettings = Field(default_factory=PromptSettings)
    context: ContextSettings = Field(default_factory=ContextSettings)
    workflow: WorkflowEngineSettings = Field(default_factory=WorkflowEngineSettings)
    cache: CacheSettings = Field(default_factory=CacheSettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)
    trace: TraceSettings = Field(default_factory=TraceSettings)

    rules_directory: str = ""
    workflows_directory: str = ""
    task_output_dir: str = Field(default_factory=_default_task_output_dir)
    rules_hot_reload: bool = True
    workflows_hot_reload: bool = True

    @property
    def workflow_queue_backend(self) -> str:
        return self.workflow.queue_backend

    @property
    def workflow_cache_mode(self) -> str:
        return self.workflow.cache_mode

    @property
    def workflow_worker_concurrency(self) -> int:
        return self.workflow.worker_concurrency

    @property
    def workflow_custom_nodes_dir(self) -> str | None:
        return self.workflow.custom_nodes_dir or None

    health_check_path: str = "/health"
    api_v1_deprecation_date: str = ""
    api_v1_sunset_date: str = ""
    api_deprecation_policy_url: str = "/docs#api-versioning"

    build_git_sha: str = Field(default="", validation_alias=AliasChoices("LEAGENT_BUILD_SHA"))
    build_time: str = Field(default="", validation_alias=AliasChoices("LEAGENT_BUILD_TIME"))

    data_root: str = Field(default="", validation_alias=AliasChoices("LEAGENT_DATA_ROOT"))
    skills_directory: str = Field(default="", validation_alias=AliasChoices("LEAGENT_SKILLS_DIR"))
    skills_github_catalog_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("LEAGENT_SKILLS_GITHUB_CATALOG_ENABLED"),
    )
    skills_github_owner: str = Field(
        default="anthropics",
        validation_alias=AliasChoices("LEAGENT_SKILLS_GITHUB_OWNER"),
    )
    skills_github_repo: str = Field(
        default="skills",
        validation_alias=AliasChoices("LEAGENT_SKILLS_GITHUB_REPO"),
    )
    skills_github_ref: str = Field(
        default="main",
        validation_alias=AliasChoices("LEAGENT_SKILLS_GITHUB_REF"),
    )
    skills_github_skills_path: str = Field(
        default="skills",
        validation_alias=AliasChoices("LEAGENT_SKILLS_GITHUB_SKILLS_PATH"),
    )
    skill_scripts_enabled: bool = Field(
        default=True,
        description=(
            "Allow run_skill_script for bundles under skills/*/scripts/. "
            "Set LEAGENT_SKILL_SCRIPTS_ENABLED=0 to disable."
        ),
    )
    skill_load_bundle_default: bool = Field(
        default=False,
        description=(
            "When true, load_skill inlines bundled references + script sources unless "
            "the caller passes include_bundled_content explicitly. "
            "Env: LEAGENT_SKILL_LOAD_BUNDLE_DEFAULT."
        ),
    )
    skill_python_deps_auto_install: bool = Field(
        default=True,
        description=(
            "When true, run_skill_script (and code_execution with skill_name) may run "
            "`uv pip install` for skill-declared deps (requirements.txt, pyproject "
            "dependencies, metadata.leagent.python_dependencies). "
            "Disable on locked-down hosts via LEAGENT_SKILL_PYTHON_DEPS_AUTO_INSTALL=0."
        ),
        validation_alias=AliasChoices("LEAGENT_SKILL_PYTHON_DEPS_AUTO_INSTALL"),
    )
    skill_python_deps_install_timeout_sec: float = Field(
        default=300.0,
        ge=5.0,
        le=3600.0,
        description=(
            "Wall-clock timeout for each uv pip install syncing a skill's Python deps."
        ),
        validation_alias=AliasChoices("LEAGENT_SKILL_PYTHON_DEPS_INSTALL_TIMEOUT_SEC"),
    )
    agent_uv_pip_install_enabled: bool = Field(
        default=True,
        description=(
            "When true, the uv_pip_install tool may run `uv pip install` into the "
            "same Python interpreter as code_execution (typically the backend venv). "
            "Disable on locked-down hosts via LEAGENT_AGENT_UV_PIP_INSTALL_ENABLED=0."
        ),
        validation_alias=AliasChoices("LEAGENT_AGENT_UV_PIP_INSTALL_ENABLED"),
    )
    backend_python_executable: str = Field(
        default="",
        description=(
            "Optional absolute Python executable for backend-managed code execution "
            "and dependency installs. When empty, LeAgent resolves "
            "UV_PROJECT_ENVIRONMENT, then backend/.venv, then sys.executable."
        ),
        validation_alias=AliasChoices("LEAGENT_BACKEND_PYTHON"),
    )
    skills_activate_all_on_start: bool = Field(
        default=True,
        description=(
            "After load_all(), activate every discovered skill so they are advertised "
            "in prompts and slash/mention UIs. "
            "Set LEAGENT_SKILLS_ACTIVATE_ALL_ON_START=0 to keep skills inactive until "
            "enabled in the skills UI or via API."
        ),
        validation_alias=AliasChoices("LEAGENT_SKILLS_ACTIVATE_ALL_ON_START"),
    )
    skills_interop_discovery_enabled: bool = Field(
        default=True,
        description=(
            "Scan well-known interop skill roots from other agents "
            "(~/.claude/skills, ~/.cursor/skills, ...) in addition to LeAgent's own "
            "skills directory. Set LEAGENT_SKILLS_INTEROP_DISCOVERY=0 to skip them "
            "when those directories are large and slow down startup."
        ),
        validation_alias=AliasChoices("LEAGENT_SKILLS_INTEROP_DISCOVERY"),
    )
    code_execution_workspace_root: str = Field(
        default="",
        validation_alias=AliasChoices("LEAGENT_CODE_EXEC_ROOT"),
    )
    code_execution_extra_import_roots: str = Field(
        default="",
        validation_alias=AliasChoices("LEAGENT_CODE_EXEC_EXTRA_IMPORT_ROOTS"),
    )
    code_execution_import_tier: str = Field(
        default="unrestricted",
        validation_alias=AliasChoices("LEAGENT_CODE_EXEC_IMPORT_TIER"),
    )
    code_execution_isolation_mode: str = Field(
        default="none",
        validation_alias=AliasChoices("LEAGENT_CODE_EXEC_ISOLATION"),
    )
    code_execution_permissive: bool = Field(
        default=True,
        validation_alias=AliasChoices("LEAGENT_CODE_EXEC_PERMISSIVE"),
    )
    code_execution_long_default_timeout_sec: float = Field(
        default=300.0,
        validation_alias=AliasChoices("LEAGENT_CODE_EXEC_LONG_DEFAULT_TIMEOUT_SEC"),
    )
    code_execution_long_max_timeout_sec: float = Field(
        default=1800.0,
        validation_alias=AliasChoices("LEAGENT_CODE_EXEC_LONG_MAX_TIMEOUT_SEC"),
    )
    code_execution_extended_default_timeout_sec: float = Field(
        default=600.0,
        validation_alias=AliasChoices("LEAGENT_CODE_EXEC_EXTENDED_DEFAULT_TIMEOUT_SEC"),
    )
    code_execution_extended_max_timeout_sec: float = Field(
        default=3600.0,
        validation_alias=AliasChoices("LEAGENT_CODE_EXEC_EXTENDED_MAX_TIMEOUT_SEC"),
    )

    # Outbound SMTP defaults for email_send / Settings UI (~/.leagent/.env via /settings/tokens).
    smtp_host: str = Field(default="", validation_alias=AliasChoices("LEAGENT_SMTP_HOST"))
    smtp_port: int = Field(default=587, validation_alias=AliasChoices("LEAGENT_SMTP_PORT"))
    smtp_username: str = Field(default="", validation_alias=AliasChoices("LEAGENT_SMTP_USERNAME"))
    smtp_password: str = Field(default="", validation_alias=AliasChoices("LEAGENT_SMTP_PASSWORD"))
    smtp_use_tls: bool = Field(default=True, validation_alias=AliasChoices("LEAGENT_SMTP_USE_TLS"))
    smtp_use_ssl: bool = Field(default=False, validation_alias=AliasChoices("LEAGENT_SMTP_USE_SSL"))
    smtp_from_email: str = Field(default="", validation_alias=AliasChoices("LEAGENT_SMTP_FROM_EMAIL"))
    smtp_from_name: str = Field(default="", validation_alias=AliasChoices("LEAGENT_SMTP_FROM_NAME"))

    coding_agent_free_shell: bool = True

    edition: str = Field(
        default="standalone",
        validation_alias=AliasChoices("LEAGENT_EDITION"),
    )
    desktop_mode: bool = Field(
        default=False,
        validation_alias=AliasChoices("LEAGENT_DESKTOP_MODE"),
    )
    local_mode: bool = Field(
        default=True,
        validation_alias=AliasChoices("LEAGENT_LOCAL_MODE"),
    )
    license_offline_registry_path: str = Field(
        default="",
        validation_alias=AliasChoices("LEAGENT_LICENSE_OFFLINE_REGISTRY_PATH"),
    )

    @property
    def is_single_machine_profile(self) -> bool:
        """Always True — this is a standalone local deployment."""
        return True


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached singleton of application settings."""
    return Settings()


__all__ = [
    "AgentSettings",
    "CacheSettings",
    "CanvasSettings",
    "CANVAS_PREVIEW_CSP_LOCAL",
    "CodingProjectsSettings",
    "ContextSettings",
    "DatabaseSettings",
    "FilesSettings",
    "ImageSearchSettings",
    "LLMSettings",
    "LogFormat",
    "PromptSettings",
    "RtspStreamSettings",
    "RuntimeProfile",
    "SecuritySettings",
    "SessionSettings",
    "Settings",
    "TraceSettings",
    "WebBrowserSettings",
    "WebFetchSettings",
    "WebSearchSettings",
    "WorkflowEngineSettings",
    "get_settings",
]
