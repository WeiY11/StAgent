# ruff: noqa: E402, I001
"""Shared pytest fixtures for the LeAgent test suite.

Intentional skips elsewhere (do not remove without product scope):

- ``test_path_sandbox.py``: strict sandbox mode; desktop builds use unrestricted
  paths — see ``test_path_sandbox_desktop.py``.
- ``test_doc_tools.py``: ``skipif`` when PyMuPDF/pandas stacks are missing (install
  ``uv sync --extra dev``, which includes the doc-tool wheels).
- ``test_code_execution.py``: unrestricted subprocess workspace boundaries (policy).

"""

from __future__ import annotations

import os
from pathlib import Path

# Before any ``import leagent.*``: ``leagent.config.constants`` snapshots
# ``LEAGENT_HOME`` at import time; default ``~/.leagent`` breaks CI / parallel
# runs and can hit permission / lock issues on Windows.
_backend_root = Path(__file__).resolve().parent.parent
_wa_pytest = _backend_root / ".pytest_leagent_home"
_wa_pytest.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("LEAGENT_HOME", str(_wa_pytest.resolve()))
# Pin the permissive test profile (loopback + auth off) at the env level so a
# ``get_settings.cache_clear()`` inside a test (e.g. to pick up monkeypatched
# SMTP vars) rebuilds Settings without flipping auth enforcement to on
# (fresh defaults bind 0.0.0.0 → effective_enforce_auth() → 401 everywhere).
os.environ.setdefault("LEAGENT_HOST", "127.0.0.1")
os.environ.setdefault("LEAGENT_SECURITY_ENFORCE_AUTH", "false")
os.environ.setdefault("LEAGENT_SECURITY_RATE_LIMIT_ENABLED", "false")
# TestClient startup uses ``SQLModel.metadata.create_all`` via DatabaseService;
# dedicated migration tests exercise Alembic directly. Skipping lifespan
# migrations keeps combined API batches deterministic on Windows.
os.environ.setdefault("LEAGENT_SKIP_STARTUP_MIGRATIONS", "true")
# Deterministic routing: a developer ``LEAGENT_FRONTEND_DIST`` would mount StaticFiles
# before API routes (405/404). Tests mount SPA last via ``mount_frontend_spa_if_configured``.
os.environ.pop("LEAGENT_FRONTEND_DIST", None)

pytest_plugins = [
    "tests.fixtures.sample_files",
    "tests.fixtures.excel_analysis",
]

import asyncio
import tempfile
import uuid
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from leagent.services.auth.service import LOCAL_USER_ID
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from leagent.agent.base import AgentConfig, AgentContext, AgentMode, AgentState
from leagent.config.settings import Settings, get_settings
from leagent.main import create_app, mount_frontend_spa_if_configured

# Force-import every model module so SQLModel.metadata sees the full schema
# (the test DB is created via ``SQLModel.metadata.create_all``). Forgetting a
# model here leads to confusing "no such table" failures in tests that only
# indirectly touch the missing entity — explicit is better than implicit.
import leagent.db.models  # noqa: F401
from leagent.tools.base import ToolContext
from leagent.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-live",
        action="store_true",
        default=False,
        help="Run tests marked live without requiring '-m live'.",
    )


def _live_tests_requested(config: pytest.Config) -> bool:
    if bool(config.getoption("--run-live", default=False)):
        return True
    mark_expr = str(getattr(config.option, "markexpr", "") or "")
    normalized = mark_expr.replace("(", " ").replace(")", " ")
    tokens = {part.strip() for part in normalized.split() if part.strip()}
    return "live" in tokens and "not live" not in mark_expr


@pytest.hookimpl(trylast=True)
def pytest_configure(config: pytest.Config) -> None:
    """Register warning filters early (belt-and-suspenders with ``pyproject.toml``).

    Ensures teardown noise from asyncio subprocess ``__del__`` and aiosqlite worker
    threads does not appear when pytest discovers config from a non-backend CWD.

    ``trylast`` so this runs *after* the builtin ``unraisableexception`` plugin has
    installed ``sys.unraisablehook`` — otherwise our wrapper is captured too early
    and immediately overwritten by pytest's own hook.
    """
    import warnings

    warnings.filterwarnings("ignore", category=pytest.PytestUnraisableExceptionWarning)
    warnings.filterwarnings("ignore", category=pytest.PytestUnhandledThreadExceptionWarning)

    _install_windows_unraisable_filter()


def pytest_collection_modifyitems(
    config: pytest.Config,
    items: list[pytest.Item],
) -> None:
    """Keep network-backed live tests opt-in even when API keys are present."""
    if _live_tests_requested(config):
        return
    skip_live = pytest.mark.skip(
        reason="live tests require explicit '-m live' or '--run-live'",
    )
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)


def _install_windows_unraisable_filter() -> None:
    """Drop known-harmless Windows asyncio transport teardown from the unraisable hook.

    pytest's ``unraisableexception`` plugin installs ``sys.unraisablehook`` and
    records every exception raised in a ``__del__`` during GC; at each test
    boundary it re-emits them as ``PytestUnraisableExceptionWarning``, which
    ``pyproject.toml`` promotes to an error — failing a *random* later test.

    On Windows, Proactor pipe/subprocess transports are routinely GC'd *after*
    their event loop closes, so their ``__del__`` emits ``ResourceWarning:
    unclosed transport`` / ``RuntimeError: Event loop is closed``. These are
    benign and never occur on the POSIX loops CI uses. Patching ``__del__`` can't
    help (the warning-as-error escapes it straight into the hook), so we wrap the
    hook itself: swallow the known-harmless transport/socket teardown noise and
    forward everything else to pytest so genuine fire-and-forget bugs still fail.
    """
    import sys

    if sys.platform != "win32":
        return

    pytest_hook = sys.unraisablehook
    harmless = (
        "unclosed transport",
        "unclosed <socket",
        "unclosed event loop",
        "Event loop is closed",
        "_ProactorBasePipeTransport.__del__",
        "BaseSubprocessTransport.__del__",
        "asyncio\\proactor_events.py",
        "asyncio\\base_subprocess.py",
    )

    import traceback as _tb

    def _filtered_hook(unraisable: Any) -> None:
        # The teardown exception itself is often a socket/repr error raised *while*
        # building the "unclosed transport" ResourceWarning, so the tell-tale text
        # lives in the traceback frames, not in ``exc_value``. Match against all of
        # err_msg + exc_value + object + the formatted traceback.
        parts = [
            getattr(unraisable, "err_msg", None) or "",
            repr(getattr(unraisable, "exc_value", None)),
            repr(getattr(unraisable, "object", None)),
        ]
        exc_tb = getattr(unraisable, "exc_traceback", None)
        if exc_tb is not None:
            parts.extend(_tb.format_tb(exc_tb))
        text = " ".join(parts)
        if any(marker in text for marker in harmless):
            return
        pytest_hook(unraisable)

    sys.unraisablehook = _filtered_hook


TEST_DB_URL = "sqlite+aiosqlite:///./test.db"


@pytest.fixture(autouse=True, scope="session")
def _widen_sandbox_for_tests():
    """Allow /tmp and system temp during tests so tmp_path works with the sandbox."""
    from leagent.file.sandbox import reset_roots

    old = os.environ.get("LEAGENT_TOOL_FILE_ROOTS")
    system_tmp = str(Path(tempfile.gettempdir()).resolve())
    os.environ["LEAGENT_TOOL_FILE_ROOTS"] = (
        f"/tmp,/tmp/leagent/files,{system_tmp}"
    )
    reset_roots()
    yield
    if old is None:
        os.environ.pop("LEAGENT_TOOL_FILE_ROOTS", None)
    else:
        os.environ["LEAGENT_TOOL_FILE_ROOTS"] = old
    reset_roots()


@pytest.fixture(scope="session")
def test_settings() -> Settings:
    """Return settings configured for the test environment."""
    settings = get_settings()
    settings.debug = True
    settings.environment = "development"
    settings.database.driver = "sqlite+aiosqlite"
    # Keep the historical permissive test profile (loopback + auth off).
    settings.host = "127.0.0.1"
    settings.security.enforce_auth = False
    settings.security.rate_limit_enabled = False
    return settings


@pytest.fixture(scope="session")
def app(test_settings: Settings):  # type: ignore[no-untyped-def]
    """Create a FastAPI application instance for testing.

    ``create_app()`` registers every router eagerly (see ``leagent.api.router``),
    so no deferred mounting is needed. SPA static files mount last via
    ``mount_frontend_spa_if_configured`` so they cannot shadow API routes.
    """
    application = create_app()

    mount_frontend_spa_if_configured(application)

    ev = asyncio.Event()
    ev.set()
    application.state.post_startup_warmup = ev

    return application


@pytest.fixture(scope="session")
def _session_client(app) -> Iterator[TestClient]:  # type: ignore[no-untyped-def]
    """Session-scoped TestClient.

    Entering ``TestClient`` runs the full app lifespan on a portal thread
    (~9s: portal start + ``start_all`` + tool bootstrap). Doing that per test
    made the ~16 ``client``-based API files dominate the whole suite (~30 min).
    Sharing one client runs that lifespan once; per-test isolation of
    ``app.dependency_overrides`` is handled by the ``client`` wrapper below.
    """
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture()
def client(_session_client, app) -> Iterator[TestClient]:  # type: ignore[no-untyped-def]
    """Synchronous test client (shares the session-scoped lifespan).

    Snapshots and restores ``app.dependency_overrides`` around each test so a
    test's overrides never leak into the next one (fixtures like ``fake_manager``
    still pop their own; this is a belt-and-suspenders safety net).
    """
    snapshot = dict(app.dependency_overrides)
    try:
        yield _session_client
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(snapshot)


@pytest_asyncio.fixture()
async def async_client(app) -> AsyncIterator[AsyncClient]:  # type: ignore[no-untyped-def]
    """Async test client using httpx."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest_asyncio.fixture()
async def db_session() -> AsyncIterator[AsyncSession]:
    """Create a temporary in-memory database session for a single test."""
    engine = create_async_engine(TEST_DB_URL, echo=False)

    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

    session_factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )

    async with session_factory() as session:
        yield session

    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)

    await engine.dispose()


@pytest.fixture()
def test_user(test_settings: Settings) -> dict[str, Any]:
    """Return a dict representing the implicit local user."""
    user_id = str(LOCAL_USER_ID)
    return {
        "user_id": user_id,
        "username": "local",
        "role": "admin",
        "department": "engineering",
        "token": "local-token",
        "auth_header": {},
    }


@pytest.fixture()
def test_viewer_user(test_settings: Settings) -> dict[str, Any]:
    user_id = str(LOCAL_USER_ID)
    return {
        "user_id": user_id,
        "username": "local",
        "role": "admin",
        "department": "general",
        "token": "local-token",
        "auth_header": {},
    }


# ---------------------------------------------------------------------------
# Tool-layer fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def tool_context() -> ToolContext:
    """Minimal ToolContext sufficient for unit-testing tools."""
    return ToolContext(
        user_id="test-user-id",
        session_id="test-session-id",
    )


@pytest.fixture()
def tool_registry() -> ToolRegistry:
    """Fresh ToolRegistry instance per test."""
    return ToolRegistry()


# ---------------------------------------------------------------------------
# Agent-layer fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def agent_config() -> AgentConfig:
    """Default agent config for tests."""
    return AgentConfig(
        max_iterations=5,
        default_timeout_sec=30,
        mode=AgentMode.REACT,
        enable_planning=False,
        enable_memory=False,
        enable_streaming=False,
        verbose=False,
    )


@pytest.fixture()
def agent_context(agent_config: AgentConfig) -> AgentContext:
    """Minimal AgentContext with mocked services."""
    ctx = AgentContext(
        task_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        config=agent_config,
        state=AgentState.IDLE,
    )
    ctx.tools = MagicMock()
    ctx.llm = MagicMock()
    ctx.agent_memory = None
    return ctx


@pytest.fixture()
def mock_llm_service() -> MagicMock:
    """Mock LLMService that returns canned responses."""
    svc = MagicMock()
    svc.complete = AsyncMock(return_value=MagicMock(
        content="mock response",
        tool_calls=None,
        usage=MagicMock(prompt_tokens=10, completion_tokens=20, total_tokens=30),
    ))
    svc.stream = AsyncMock(return_value=iter([]))
    return svc


@pytest.fixture()
def mock_registry() -> MagicMock:
    """Mock ToolRegistry."""
    reg = MagicMock(spec=ToolRegistry)
    reg.get = MagicMock(return_value=None)
    reg.list_all = MagicMock(return_value=[])
    return reg
