"""StAgent CLI entry point.

Commands fall into three buckets:

**Local agent (no HTTP server)** -bootstraps :class:`~StAgent.llm.service.LLMService`,
the unified :class:`~StAgent.tools.registry.ToolRegistry`, rules, and skills, then runs
turns through :class:`~StAgent.agent.controller.AgentController` (the compatibility façade
over :class:`~StAgent.agent.query_engine.QueryEngine`). Prompts are assembled via
:class:`~StAgent.prompts.builder.PromptBuilder` and template layers when a provider is
configured.

**FastAPI monolith** -``StAgent run`` / ``StAgent app start`` serve ``StAgent.main:app``
(Uvicorn or Gunicorn): REST, SSE, WebSockets, :class:`~StAgent.services.session.manager.SessionManager`,
optional :class:`~StAgent.memory.agent_memory.AgentMemory`, workflow engine, cron, files, admin.

**Server API clients** -``workflows``, ``tasks``, ``chats`` (and similar) call
``StAgent_API_URL`` (default ``http://localhost:7860``) with optional ``StAgent_API_KEY``.

Examples:
    StAgent                     # Interactive local agent (REPL)
    StAgent chat                # Same as bare ``StAgent``
    StAgent -m "-              # One-shot local agent message
    StAgent init                # ~/.StAgent layout + config templates
    StAgent run                  # Uvicorn ASGI (shortcut for ``app start``)
    StAgent app start            # Same stack with extra flags (SSL, workers, -
    StAgent workflows list       # Requires running server
    StAgent models migrate      # one-shot tier ->v2 task routing migration
    StAgent doctor               # Health / dependency sanity check
"""

from __future__ import annotations

import click

from StAgent.cli.app_cmd import app_group
from StAgent.cli.auth_cmd import reset_password_cmd
from StAgent.cli.channels_cmd import channels_group
from StAgent.cli.chat_cmd import chat_cmd
from StAgent.cli.chats_cmd import chats_group
from StAgent.cli.clean_cmd import clean_cmd, prune_cmd
from StAgent.cli.config_cmd import config_group
from StAgent.cli.cron_cmd import cron_group
from StAgent.cli.daemon_cmd import daemon_group
from StAgent.cli.env_cmd import env_group
from StAgent.cli.init_cmd import init_cmd
from StAgent.cli.providers_cmd import models_group
from StAgent.cli.rules_cmd import rules_group
from StAgent.cli.skills_cmd import skills_group
from StAgent.cli.tasks_cmd import tasks_group
from StAgent.cli.templates_cmd import templates_group
from StAgent.cli.utils import console, print_error, print_success
from StAgent.cli.webhooks_cmd import webhooks_group
from StAgent.cli.workflows_cmd import workflows_group


class AgenticGroup(click.Group):
    """Root Click group: default action is the local agent REPL (not ``--help``).

    With no subcommand, the CLI starts :mod:`StAgent.cli.chat_cmd` -the same
    QueryEngine-backed controller path as the web chat, but without
    ``SessionManager`` persistence or signed attachment URLs unless you add them.
    """

    def invoke(self, ctx: click.Context) -> None:
        if not ctx.protected_args and not ctx.invoked_subcommand:
            # Check for -m/--message flag (one-shot mode)
            message = ctx.params.get("message")
            if message:
                from StAgent.cli.chat_cmd import run_one_shot
                run_one_shot(
                    message,
                    verbose=ctx.params.get("verbose_chat", False),
                    debug=ctx.params.get("debug", False),
                )
                return
            # No subcommand and no message -enter interactive mode
            ctx.invoke(chat_cmd, verbose=ctx.params.get("verbose_chat", False))
            return
        super().invoke(ctx)


@click.group(cls=AgenticGroup, invoke_without_command=True)
@click.version_option(package_name="StAgent")
@click.option("--debug", is_flag=True, help="Enable debug mode with verbose output.")
@click.option(
    "-m", "--message",
    default=None,
    help="Run a single message in non-interactive mode.",
)
@click.option(
    "-v", "--verbose-chat",
    is_flag=True,
    help="Enable verbose agent output.",
)
@click.pass_context
def cli(ctx: click.Context, debug: bool, message: str | None, verbose_chat: bool) -> None:
    """StAgent -local-first agents, workflows, rules, and skills.

    **Local agent (default)** -No server: LLM + tools + rules + skills in-process.

    **Platform** -``init`` seeds ``~/.StAgent``; ``run`` / ``app start`` / ``serve``
    expose the FastAPI app; ``upgrade`` applies Alembic migrations (packaged under
    ``StAgent/alembic``, no repo ``alembic.ini`` required).

    **Operators** -``workflows``, ``tasks``, ``chats``, ``cron``, -talk to
    ``StAgent_API_URL`` (start the server first unless a command documents a local fallback).
    ``reset-password`` force-resets the access password (or a named user) locally.

    \b
    Quick start:
      StAgent                  # Interactive agent REPL
      StAgent -m "-           # One-shot agent turn
      StAgent init             # First-time ~/.StAgent + templates
      StAgent run              # HTTP API (Uvicorn)
      StAgent app start        # Same with SSL / Gunicorn / reload flags
      StAgent models list      # providers.yaml + model tiers
      StAgent doctor           # Dependency and config checks
      StAgent reset-password   # Force-reset forgotten access password

    \b
    Docs: https://github.com/WeiY11/StAgent
    """
    ctx.ensure_object(dict)
    ctx.obj["debug"] = debug

    from StAgent.utils.logging import setup_logging

    setup_logging(level="DEBUG" if debug else "INFO", log_format="console")


# ── Register command groups ─────────────────────────────────────────

cli.add_command(chat_cmd)
cli.add_command(app_group)
cli.add_command(init_cmd)
cli.add_command(models_group)
cli.add_command(channels_group)
cli.add_command(skills_group)
cli.add_command(cron_group)
cli.add_command(chats_group)
cli.add_command(env_group)
cli.add_command(daemon_group)
cli.add_command(config_group)
cli.add_command(rules_group)
cli.add_command(workflows_group)
cli.add_command(tasks_group)
cli.add_command(templates_group)
cli.add_command(webhooks_group)
cli.add_command(clean_cmd)
cli.add_command(prune_cmd)
cli.add_command(reset_password_cmd)


# ── Server commands ─────────────────────────────────────────────────

@cli.command(name="run")
@click.option("--host", default="0.0.0.0", help="Bind host")
@click.option("--port", default=7860, type=int, help="Bind port")
@click.option("--workers", default=1, type=int, help="Number of workers")
@click.option("--reload", is_flag=True, help="Enable auto-reload (dev only)")
@click.option("--log-level", default="info", help="Log level")
def run(host: str, port: int, workers: int, reload: bool, log_level: str) -> None:
    """Start the FastAPI monolith (``StAgent.main:app``) with Uvicorn.

    Equivalent to ``StAgent app start`` without SSL or the ``--production`` switch.
    """
    from StAgent.server import run_uvicorn

    console.print(f"[bold green]Starting StAgent on {host}:{port}[/]")
    run_uvicorn(host=host, port=port, workers=workers, reload=reload, log_level=log_level)


@cli.command(name="serve")
@click.option("--host", default="0.0.0.0", help="Bind host")
@click.option("--port", default=7860, type=int, help="Bind port")
@click.option("--workers", default=None, type=int, help="Number of Gunicorn workers")
@click.option("--log-level", default="info", help="Log level")
def serve(host: str, port: int, workers: int | None, log_level: str) -> None:
    """Start the FastAPI app with Gunicorn + ``uvicorn.workers.UvicornWorker`` (production)."""
    from StAgent.server import run_gunicorn

    console.print(f"[bold green]Starting StAgent (Gunicorn) on {host}:{port}[/]")
    run_gunicorn(host=host, port=port, workers=workers, log_level=log_level)


# ── Database commands ───────────────────────────────────────────────

@cli.command()
@click.argument("message")
def migrate(message: str) -> None:
    """Generate a new Alembic revision (autogenerate) using the packaged ``StAgent/alembic`` env."""
    from alembic import command

    from StAgent.alembic.runtime_config import load_alembic_config

    alembic_cfg = load_alembic_config()
    command.revision(alembic_cfg, message=message, autogenerate=True)
    print_success(f"Migration created: {message}")


@cli.command()
@click.option("--revision", "-r", default="head", help="Target revision (default: head)")
def upgrade(revision: str) -> None:
    """Apply Alembic migrations to the configured database (``DB_*`` / ``DATABASE_URL``)."""
    from alembic import command

    from StAgent.alembic.runtime_config import load_alembic_config

    alembic_cfg = load_alembic_config()
    command.upgrade(alembic_cfg, revision)
    print_success(f"Database upgraded to {revision}.")


@cli.command()
@click.option("--revision", "-r", default="-1", help="Target revision (default: -1)")
def downgrade(revision: str) -> None:
    """Revert Alembic migrations (destructive -use with care)."""
    from alembic import command

    from StAgent.alembic.runtime_config import load_alembic_config

    alembic_cfg = load_alembic_config()
    command.downgrade(alembic_cfg, revision)
    print_success(f"Database downgraded to {revision}.")


# ── Info / diagnostics ──────────────────────────────────────────────

@cli.command()
def version() -> None:
    """Show StAgent version and system information."""
    import platform
    import sys

    try:
        from importlib.metadata import version as get_version
        StAgent_version = get_version("StAgent")
    except Exception:
        StAgent_version = "unknown"

    console.print()
    console.print("[bold cyan]StAgent[/]")
    console.print()
    console.print(f"  Version:  {StAgent_version}")
    console.print(f"  Python:   {sys.version.split()[0]}")
    console.print(f"  Platform: {platform.system()} {platform.release()}")
    console.print(f"  Machine:  {platform.machine()}")
    console.print()


@cli.command()
def doctor() -> None:
    """Check local install health: config paths, core imports, LLM registry, tools, rules, skills."""
    import shutil
    from pathlib import Path

    from StAgent.config.constants import (
        ALL_DIRS,
        CONFIG_PATH,
        StAgent_HOME,
        PROVIDERS_PATH,
        RULES_DIR,
    )
    from StAgent.config.settings import get_settings

    console.print()
    console.rule("[bold cyan]StAgent Health Check[/]")
    console.print()

    all_ok = True

    # Configuration
    console.print("[bold]Configuration:[/]")
    if CONFIG_PATH.exists():
        console.print(f"  [green]✓[/] Config file exists: {CONFIG_PATH}")
    else:
        console.print(f"  [red]✗[/] Config file missing: {CONFIG_PATH}")
        all_ok = False

    if PROVIDERS_PATH.exists():
        console.print(f"  [green]✓[/] Providers config exists: {PROVIDERS_PATH}")
    else:
        console.print(f"  [yellow]○[/] Providers config missing: {PROVIDERS_PATH}")

    # Project config
    from StAgent.cli.config_cmd import find_project_dir
    project_dir = find_project_dir()
    if project_dir:
        console.print(f"  [green]✓[/] Project config: {project_dir}")
    else:
        console.print(f"  [dim]○[/] No project .StAgent/ directory")

    # Directories
    console.print()
    console.print("[bold]Directories:[/]")
    for d in ALL_DIRS[:5]:
        if d.exists():
            console.print(f"  [green]✓[/] {d}")
        else:
            console.print(f"  [yellow]○[/] {d} (will be created)")

    # Dependencies
    console.print()
    console.print("[bold]Dependencies:[/]")
    deps = [
        ("uvicorn", "ASGI server"),
        ("fastapi", "Web framework"),
        ("httpx", "HTTP client"),
        ("rich", "Terminal formatting"),
        ("pydantic", "Data validation"),
    ]
    for pkg, desc in deps:
        try:
            __import__(pkg)
            console.print(f"  [green]✓[/] {pkg}: {desc}")
        except ImportError:
            console.print(f"  [red]✗[/] {pkg}: {desc} (not installed)")
            all_ok = False

    optional_deps = [
        ("psutil", "Process monitoring"),
        ("croniter", "Cron expression parsing"),
        ("gunicorn", "Production server"),
    ]
    console.print()
    console.print("[bold]Optional Dependencies:[/]")
    for pkg, desc in optional_deps:
        try:
            __import__(pkg)
            console.print(f"  [green]✓[/] {pkg}: {desc}")
        except ImportError:
            console.print(f"  [dim]○[/] {pkg}: {desc} (not installed)")

    # Agent readiness
    console.print()
    console.print("[bold]Agent runtime (local):[/]")

    try:
        from StAgent.llm.service import LLMService
        svc = LLMService.from_settings()
        providers = svc.list_providers()
        console.print(f"  [green]✓[/] LLM service: {len(providers)} provider(s)")
    except Exception:
        console.print("  [yellow]○[/] LLM service: not configured")

    try:
        from StAgent.tools.registry import get_registry
        registry = get_registry()
        count = registry.discover_all()
        console.print(f"  [green]✓[/] Tool registry: {count} tools discovered")
    except Exception:
        console.print("  [yellow]○[/] Tool registry: discovery failed")

    # Rules
    rule_count = 0
    raw_rules = (get_settings().rules_directory or "").strip()
    rules_path = Path(raw_rules).expanduser().resolve() if raw_rules else RULES_DIR
    if rules_path.exists():
        yaml_files = list(rules_path.glob("*.yaml")) + list(rules_path.glob("*.yml"))
        rule_count = len(yaml_files)
    console.print(f"  {'[green]✓[/]' if rule_count else '[dim]○[/]'} Rules: {rule_count} file(s) in {rules_path}")

    # Skills
    raw_skills = (get_settings().skills_directory or "").strip()
    skills_dir = Path(raw_skills).expanduser().resolve() if raw_skills else StAgent_HOME / "skills"
    skill_count = 0
    if skills_dir.exists():
        skill_count = sum(1 for d in skills_dir.iterdir() if d.is_dir() and not d.name.startswith("."))
    console.print(f"  {'[green]✓[/]' if skill_count else '[dim]○[/]'} Skills: {skill_count} installed")

    # Workflow templates
    try:
        from StAgent.workflow.template_service import TemplateService
        tsvc = TemplateService()
        templates = tsvc.list_templates()
        console.print(f"  [green]✓[/] Workflow templates: {len(templates)} available")
    except Exception:
        console.print("  [dim]○[/] Workflow templates: not loaded")

    # MCP servers
    try:
        from StAgent.config.config import load_config as load_runtime_config
        runtime_cfg = load_runtime_config()
        mcp_count = len(runtime_cfg.mcp_servers) if runtime_cfg.mcp_servers else 0
        console.print(f"  {'[green]✓[/]' if mcp_count else '[dim]○[/]'} MCP servers: {mcp_count} configured")
    except Exception:
        console.print("  [dim]○[/] MCP: not configured")

    # Server & services
    console.print()
    console.print("[bold]Server & Services:[/]")
    import httpx
    try:
        response = httpx.get("http://localhost:7860/health", timeout=2.0)
        if response.status_code == 200:
            console.print("  [green]✓[/] StAgent server: running")
            data = response.json()
            if data.get("version"):
                console.print(f"      Version: {data['version']}")
        else:
            console.print("  [yellow]○[/] StAgent server: not responding")
    except Exception:
        console.print("  [dim]○[/] StAgent server: not running")

    # Database
    try:
        from StAgent.config.settings import get_settings
        settings = get_settings()
        db_url = settings.database.url if hasattr(settings, "database") else None
        if db_url:
            console.print(f"  [green]✓[/] Database: configured")
        else:
            console.print("  [yellow]○[/] Database: not configured")
    except Exception:
        console.print("  [dim]○[/] Database: check settings")

    # Redis
    try:
        response = httpx.get("http://localhost:7860/api/v1/health/detailed", timeout=2.0)
        if response.status_code == 200:
            health_data = response.json()
            for svc_name in ("redis", "milvus", "minio", "cron"):
                svc_status = health_data.get("services", {}).get(svc_name, {})
                if svc_status.get("status") == "healthy":
                    console.print(f"  [green]✓[/] {svc_name.title()}: healthy")
                elif svc_status:
                    console.print(f"  [yellow]○[/] {svc_name.title()}: {svc_status.get('status', 'unknown')}")
    except Exception:
        pass

    console.print()
    if all_ok:
        print_success("All checks passed!")
    else:
        print_error("Some checks failed. Run 'StAgent init' to fix configuration issues.")


# ── Workflow execution ──────────────────────────────────────────────

@cli.command()
@click.argument("flow_id")
@click.option("--input", "-i", "input_file", type=click.Path(exists=True), help="Input JSON file")
@click.option("--param", "-p", multiple=True, help="Parameters as key=value pairs")
def execute(flow_id: str, input_file: str | None, param: tuple[str, ...]) -> None:
    """POST ``/api/v1/workflow/flows/{id}/run`` -shortcut for ``StAgent workflows run``."""
    import json

    from StAgent.cli.http import CLIHttpError, get_client

    inputs: dict = {}
    if input_file:
        with open(input_file, encoding="utf-8") as f:
            inputs = json.load(f)

    for p in param:
        if "=" in p:
            key, value = p.split("=", 1)
            inputs[key] = value

    console.print(f"Executing flow [cyan]{flow_id}[/]...")

    try:
        client = get_client()
        result = client.post(
            f"/api/v1/workflow/flows/{flow_id}/run",
            json={"inputs": inputs},
        )
        execution_id = result.get("execution_id", result.get("id"))
        if execution_id:
            print_success(f"Execution started: {execution_id}")
        else:
            print_success("Flow executed.")
        if result.get("outputs"):
            console.print_json(data=result["outputs"])
    except CLIHttpError as e:
        print_error(f"Execution failed: {e}")
        raise click.Abort()


# ── Interactive shell ───────────────────────────────────────────────

@cli.command()
def shell() -> None:
    """Start a Python REPL with ``load_config()``, ``get_client()``, and ``console`` in scope."""
    try:
        from IPython import embed

        banner = """
StAgent Interactive Shell
===========================
Available objects:
  - config: Runtime configuration
  - client: HTTP client for API calls
  - console: Rich console for output

Type 'help(obj)' for documentation.
"""
        from StAgent.cli.http import get_client
        from StAgent.config.config import load_config

        config = load_config()
        client = get_client()

        embed(banner1=banner, colors="neutral")

    except ImportError:
        import code

        from StAgent.cli.http import get_client
        from StAgent.config.config import load_config

        config = load_config()
        client = get_client()

        banner = "StAgent Interactive Shell\nObjects: config, client, console"
        code.interact(banner=banner, local={"config": config, "client": client, "console": console})


def main() -> None:
    """Console script entrypoint registered as ``StAgent`` in ``pyproject.toml``."""
    cli()


if __name__ == "__main__":
    main()
