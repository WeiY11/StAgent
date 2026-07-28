"""Application-wide constants and enumerations.

This package re-exports every name from its domain sub-modules so that
existing imports like ``from leagent.config.constants import TOOL_CATEGORIES``
continue to work without modification.
"""

from __future__ import annotations

from leagent.config.constants.agent import (
    AGENT_BROWSER_POOL_SIZE,
    AGENT_CONTEXT_WINDOW_TOKENS,
    AGENT_DEFAULT_TIMEOUT_SEC,
    AGENT_MAX_ITERATIONS,
    AGENT_MAX_OUTPUT_TOKENS,
    AGENT_MAX_TOOL_CALLS_PER_TURN,
    TIER1_TRIGGERS,
    TIER2_TRIGGERS,
)
from leagent.config.constants.auth import (
    JWT_ACCESS_TOKEN_EXPIRE_MINUTES,
    JWT_ALGORITHM,
    JWT_ISSUER,
    JWT_REFRESH_TOKEN_EXPIRE_MINUTES,
    LOCAL_USER_ID,
    RBAC_ROLES,
)
from leagent.config.constants.files import (
    ALL_SUPPORTED_TYPES,
    MAX_UPLOAD_SIZE_BYTES,
    MAX_UPLOAD_SIZE_MB,
    SUPPORTED_ARCHIVE_TYPES,
    SUPPORTED_DOCUMENT_TYPES,
    SUPPORTED_IMAGE_TYPES,
)
from leagent.config.constants.misc import (
    DEFAULT_PAGINATION_LIMIT,
    MAX_PAGINATION_LIMIT,
    SSE_KEEPALIVE_INTERVAL,
    WEBSOCKET_HEARTBEAT_INTERVAL,
)
from leagent.config.constants.paths import (
    ALL_DIRS,
    CACHE_DIR,
    CHAT_PROJECTS_FILES_ROOT,
    CODE_EXEC_ROOT,
    CONFIG_PATH,
    JOBS_PATH,
    KNOWLEDGE_DIR,
    LEAGENT_HOME,
    LOG_DIR,
    OUTPUT_DIR,
    PROVIDERS_PATH,
    RULES_DIR,
    SECRET_DIR,
    TASK_OUTPUT_DIR,
    TEMP_DIR,
    TEMPLATE_DIR,
    TRACES_DIR,
    UPLOAD_DIR,
    WORKFLOWS_DIR,
    WORKING_DIR,
)
from leagent.config.constants.tools import (
    TOOL_CATEGORIES,
)
from leagent.config.constants.workflow import (
    BUSINESS_MODULES,
    RULE_TYPES,
    WORKFLOW_NODE_TYPES,
)

__all__ = [
    # agent
    "AGENT_BROWSER_POOL_SIZE",
    "AGENT_CONTEXT_WINDOW_TOKENS",
    "AGENT_DEFAULT_TIMEOUT_SEC",
    "AGENT_MAX_ITERATIONS",
    "AGENT_MAX_OUTPUT_TOKENS",
    "AGENT_MAX_TOOL_CALLS_PER_TURN",
    "TIER1_TRIGGERS",
    "TIER2_TRIGGERS",
    # auth
    "JWT_ACCESS_TOKEN_EXPIRE_MINUTES",
    "JWT_ALGORITHM",
    "JWT_ISSUER",
    "JWT_REFRESH_TOKEN_EXPIRE_MINUTES",
    "LOCAL_USER_ID",
    "RBAC_ROLES",
    # files
    "ALL_SUPPORTED_TYPES",
    "MAX_UPLOAD_SIZE_BYTES",
    "MAX_UPLOAD_SIZE_MB",
    "SUPPORTED_ARCHIVE_TYPES",
    "SUPPORTED_DOCUMENT_TYPES",
    "SUPPORTED_IMAGE_TYPES",
    # misc
    "DEFAULT_PAGINATION_LIMIT",
    "MAX_PAGINATION_LIMIT",
    "SSE_KEEPALIVE_INTERVAL",
    "WEBSOCKET_HEARTBEAT_INTERVAL",
    # paths
    "ALL_DIRS",
    "CACHE_DIR",
    "CHAT_PROJECTS_FILES_ROOT",
    "CODE_EXEC_ROOT",
    "CONFIG_PATH",
    "JOBS_PATH",
    "KNOWLEDGE_DIR",
    "LEAGENT_HOME",
    "LOG_DIR",
    "OUTPUT_DIR",
    "PROVIDERS_PATH",
    "RULES_DIR",
    "SECRET_DIR",
    "TASK_OUTPUT_DIR",
    "TEMPLATE_DIR",
    "TEMP_DIR",
    "TRACES_DIR",
    "UPLOAD_DIR",
    "WORKING_DIR",
    "WORKFLOWS_DIR",
    # tools
    "TOOL_CATEGORIES",
    # workflow
    "BUSINESS_MODULES",
    "RULE_TYPES",
    "WORKFLOW_NODE_TYPES",
]
