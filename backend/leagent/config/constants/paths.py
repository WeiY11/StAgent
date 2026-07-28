"""Directory layout constants."""

from __future__ import annotations

import os
from pathlib import Path

# ---------------------------------------------------------------------------
# Directory layout
# ---------------------------------------------------------------------------

LEAGENT_HOME = Path(os.getenv("LEAGENT_HOME", Path.home() / ".leagent"))
WORKING_DIR = LEAGENT_HOME / "working"
SECRET_DIR = LEAGENT_HOME / "secrets"
CONFIG_PATH = LEAGENT_HOME / "config.yaml"
PROVIDERS_PATH = LEAGENT_HOME / "providers.yaml"
JOBS_PATH = LEAGENT_HOME / "jobs"

UPLOAD_DIR = WORKING_DIR / "uploads"
OUTPUT_DIR = WORKING_DIR / "outputs"
TEMP_DIR = WORKING_DIR / "tmp"
LOG_DIR = LEAGENT_HOME / "logs"
TEMPLATE_DIR = LEAGENT_HOME / "templates"
RULES_DIR = LEAGENT_HOME / "rules"
WORKFLOWS_DIR = LEAGENT_HOME / "workflows"
CACHE_DIR = WORKING_DIR / "cache"
# Subprocess ``code_execution`` sandboxes (per-session dirs under this root).
CODE_EXEC_ROOT = WORKING_DIR / "code-exec"
# Shared on-disk roots for chat projects (``working/projects/<project_id>/``).
CHAT_PROJECTS_FILES_ROOT = WORKING_DIR / "projects"
# Indexed knowledge / document blobs (default; override via ``FilesSettings.knowledge_storage_dir``).
KNOWLEDGE_DIR = LEAGENT_HOME / "knowledge"
# Out-of-line agent trace payloads (opt-in via LEAGENT_TRACE_CAPTURE_PAYLOADS).
TRACES_DIR = LEAGENT_HOME / "traces"
# Task output files (per-task result logs).
TASK_OUTPUT_DIR = LEAGENT_HOME / "tasks"

ALL_DIRS = [
    LEAGENT_HOME,
    WORKING_DIR,
    SECRET_DIR,
    UPLOAD_DIR,
    OUTPUT_DIR,
    TEMP_DIR,
    LOG_DIR,
    TEMPLATE_DIR,
    RULES_DIR,
    WORKFLOWS_DIR,
    JOBS_PATH,
    CACHE_DIR,
    CODE_EXEC_ROOT,
    CHAT_PROJECTS_FILES_ROOT,
    KNOWLEDGE_DIR,
    TRACES_DIR,
    TASK_OUTPUT_DIR,
]
