"""StAgent CLI package.

Command groups:

**Local-first (no server process)** — ``chat`` / bare ``StAgent`` (interactive
``QueryEngine``-backed turns via :class:`~StAgent.agent.controller.AgentController`),
``init`` (``~/.StAgent``), ``rules``, ``skills``, ``models`` (``providers.yaml``),
``config`` (per-repo ``.StAgent/``), ``channels`` (runtime YAML), ``env``, ``clean``,
``prune``, ``doctor``, ``reset-password``, Alembic helpers (``migrate``, ``upgrade``,
``downgrade``).

**ASGI process** — ``run``, ``serve``, ``app`` (Uvicorn / Gunicorn for ``StAgent.main:app``).

**HTTP API clients** — ``workflows``, ``tasks``, ``chats``, ``cron``, ``templates``,
``webhooks`` (``StAgent_API_URL``, optional ``StAgent_API_KEY``).

**Background** — ``daemon`` (PID file under ``StAgent_HOME``).

**Utilities** — ``execute`` (workflow run shortcut), ``shell`` (IPython / stdlib),
``version``, one-shot ``-m``.
"""

from StAgent.cli.http import CLIHttpClient, CLIHttpError, get_client
from StAgent.cli.main import cli, main
from StAgent.cli.utils import (
    console,
    create_table,
    format_bytes,
    format_duration,
    print_error,
    print_info,
    print_success,
    print_warning,
    prompt_confirm,
    prompt_text,
    status_badge,
)

__all__ = [
    "cli",
    "main",
    "console",
    "create_table",
    "format_bytes",
    "format_duration",
    "print_error",
    "print_info",
    "print_success",
    "print_warning",
    "prompt_confirm",
    "prompt_text",
    "status_badge",
    "CLIHttpClient",
    "CLIHttpError",
    "get_client",
]
