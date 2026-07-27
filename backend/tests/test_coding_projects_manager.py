"""Unit tests for :class:`CodingProjectManager` scaffolding logic.

Covers:

* template loader (toml metadata + files copy)
* port allocator (alloc / release / re-use)
* preview-token mint + decode
* :func:`PortAllocator` does not double-lease
* manager scaffold flow: row + matching project-mode Folder

These are pure-Python tests — no FastAPI app, no real subprocesses.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from leagent.config.settings import Settings, get_settings
from leagent.db.models import (
    CodingProject,
    CodingProjectStatus,
    Folder,
)
from leagent.project.manager import CodingProjectManager, CodingProjectNotFoundError
from leagent.project.ports import (
    PortAllocationError,
    PortAllocator,
)
from leagent.project.preview_tokens import (
    PREVIEW_AUDIENCE,
    decode_preview_token,
    mint_preview_token,
    preview_query_path,
)
from leagent.project.templates import (
    TemplateNotFoundError,
    list_templates,
    load_template,
)
from leagent.services.auth.tokens import TokenError, mint_token

# ---------------------------------------------------------------------------
# Fake DatabaseService
# ---------------------------------------------------------------------------


class _SessionCM:
    def __init__(self, factory):
        self._factory = factory
        self._session = None

    async def __aenter__(self) -> AsyncSession:
        self._session = self._factory()
        return self._session

    async def __aexit__(self, exc_type, exc, tb) -> None:
        try:
            if exc is None:
                await self._session.commit()
            else:
                await self._session.rollback()
        finally:
            await self._session.close()


class FakeDB:
    def __init__(self, factory) -> None:
        self._factory = factory

    def session(self) -> _SessionCM:
        return _SessionCM(self._factory)


class RecordingSupervisor:
    def __init__(self) -> None:
        self.stopped: list[object] = []

    async def stop(self, project_id, *args, **kwargs) -> None:
        self.stopped.append(project_id)


class RecordingPorts:
    def __init__(self) -> None:
        self.released: list[str] = []

    def release(self, lease_key: str) -> None:
        self.released.append(lease_key)


async def _insert_dashed_coding_project(
    db: FakeDB,
    *,
    project_id: UUID,
    folder_id: UUID,
    user_id: UUID,
    root_path: Path,
) -> None:
    now = datetime.utcnow().isoformat(sep=" ")
    async with db.session() as session:
        await session.execute(
            text(
                "INSERT INTO coding_projects "
                "(is_deleted, created_at, updated_at, id, name, template, runtime_kind, "
                "user_id, folder_id, root_path, status) "
                "VALUES (0, :now, :now, :id, 'demo', 'vanilla-html', 'frontend', "
                ":uid, :folder_id, :root_path, 'idle')"
            ),
            {
                "now": now,
                "id": str(project_id),
                "uid": str(user_id),
                "folder_id": str(folder_id),
                "root_path": str(root_path),
            },
        )


@pytest_asyncio.fixture()
async def fake_db() -> FakeDB:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: SQLModel.metadata.create_all(
                sync_conn,
                tables=[Folder.__table__, CodingProject.__table__],
            )
        )
    factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    yield FakeDB(factory)
    await engine.dispose()


@pytest.fixture()
def tmp_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("CODING_PROJECTS_ROOT", str(tmp_path / "scratch"))
    monkeypatch.setenv("CODING_PROJECTS_BIND_HOST", "127.0.0.1")
    monkeypatch.setenv("CODING_PROJECTS_PORT_RANGE_MIN", "0")
    monkeypatch.setenv("CODING_PROJECTS_PORT_RANGE_MAX", "65000")
    settings = get_settings()
    # Refresh: pydantic BaseSettings caches by default; reload our subsection
    settings.coding_projects.root = str(tmp_path / "scratch")
    settings.coding_projects.bind_host = "127.0.0.1"
    settings.coding_projects.port_range_min = 49000
    settings.coding_projects.port_range_max = 49099
    return settings


# ---------------------------------------------------------------------------
# Template loader
# ---------------------------------------------------------------------------


def test_list_templates_includes_builtins() -> None:
    names = {t.name for t in list_templates()}
    assert {"vanilla-html", "vite-react", "fastapi"}.issubset(names)


def test_load_template_missing_raises() -> None:
    with pytest.raises(TemplateNotFoundError):
        load_template("does-not-exist")


def test_template_argv_expand_replaces_port_and_host() -> None:
    tmpl = load_template("vanilla-html")
    expanded = tmpl.expand_argv(tmpl.start_argv, host="127.0.0.1", port=49321)
    assert "49321" in " ".join(expanded)
    assert "127.0.0.1" in " ".join(expanded)
    assert "$PORT" not in " ".join(expanded)
    assert "$HOST" not in " ".join(expanded)


def test_vite_template_mounts_at_preview_base() -> None:
    tmpl = load_template("vite-react")
    assert tmpl.uses_preview_base()
    expanded = tmpl.expand_argv(
        tmpl.start_argv,
        host="127.0.0.1",
        port=49321,
        preview_base="/api/v1/coding-projects/x/preview/",
    )
    joined = " ".join(expanded)
    assert "--base /api/v1/coding-projects/x/preview/" in joined
    assert "$PREVIEW_BASE" not in joined


# ---------------------------------------------------------------------------
# Port allocator
# ---------------------------------------------------------------------------


def test_port_allocator_returns_distinct_ports() -> None:
    alloc = PortAllocator(host="127.0.0.1", low=49500, high=49550)
    p1 = alloc.allocate("a")
    p2 = alloc.allocate("b")
    assert p1 != p2
    assert 49500 <= p1 <= 49550
    assert 49500 <= p2 <= 49550

    # Re-allocating the same lease key returns the same port.
    assert alloc.allocate("a") == p1

    alloc.release("a")
    assert "a" not in set(alloc.held_ports)


def test_port_allocator_raises_when_range_exhausted() -> None:
    alloc = PortAllocator(host="127.0.0.1", low=49600, high=49600)
    alloc.allocate("a")
    # The single-slot range is now exhausted under the lease table.
    with pytest.raises(PortAllocationError):
        alloc.allocate("b")


# ---------------------------------------------------------------------------
# Preview tokens
# ---------------------------------------------------------------------------


def test_mint_and_decode_preview_token_roundtrip(tmp_settings: Settings) -> None:
    project_id = uuid4()
    user_id = uuid4()
    token = mint_preview_token(
        tmp_settings,
        project_id=project_id,
        run_seq=2,
        user_id=user_id,
        ttl_seconds=120,
    )
    claims = decode_preview_token(tmp_settings, token)
    assert claims["cpid"] == str(project_id)
    assert claims["run"] == 2
    assert claims["sub"] == str(user_id)
    assert claims["aud"] == PREVIEW_AUDIENCE


def test_decode_preview_token_rejects_wrong_audience(tmp_settings: Settings) -> None:
    payload = {
        "cpid": str(uuid4()),
        "run": 1,
        "sub": str(uuid4()),
        "iat": 0,
        "exp": 9_999_999_999,
        "aud": "wrong-audience",
    }
    bogus = mint_token(payload, "leagent-local-secret")
    with pytest.raises(TokenError):
        decode_preview_token(tmp_settings, bogus)


def test_preview_query_path_includes_token_and_id() -> None:
    pid = uuid4()
    url = preview_query_path(pid, "abc.def.ghi", sub_path="items?x=1")
    # sub_path contains a query — preview_query_path joins via path; we
    # just assert the project id and token live in the result.
    assert str(pid) in url
    assert "token=abc.def.ghi" in url


# ---------------------------------------------------------------------------
# Manager scaffold
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scaffold_creates_files_and_folder(
    fake_db: FakeDB, tmp_settings: Settings
) -> None:
    manager = CodingProjectManager(tmp_settings, fake_db)  # type: ignore[arg-type]
    user_id = uuid4()
    project = await manager.scaffold(
        user_id=user_id,
        name="hello",
        template="vanilla-html",
    )
    assert project.template == "vanilla-html"
    assert project.status == CodingProjectStatus.IDLE
    root = Path(project.root_path)
    assert root.is_dir()
    assert (root / "index.html").is_file()

    async with fake_db.session() as session:  # type: ignore[attr-defined]
        from sqlmodel import select

        folders = (await session.exec(select(Folder))).all()
        projects = (await session.exec(select(CodingProject))).all()
    assert len(folders) == 1
    assert folders[0].is_project is True
    assert folders[0].project_path == project.root_path
    assert len(projects) == 1


@pytest.mark.asyncio
async def test_delete_rejects_foreign_project_before_stopping_runtime(
    fake_db: FakeDB, tmp_settings: Settings
) -> None:
    supervisor = RecordingSupervisor()
    ports = RecordingPorts()
    manager = CodingProjectManager(
        tmp_settings,
        fake_db,  # type: ignore[arg-type]
        supervisor=supervisor,  # type: ignore[arg-type]
        ports=ports,  # type: ignore[arg-type]
    )
    owner = uuid4()
    attacker = uuid4()
    project = await manager.scaffold(
        user_id=owner,
        name="owned",
        template="vanilla-html",
    )

    with pytest.raises(CodingProjectNotFoundError):
        await manager.delete(project_id=project.id, user_id=attacker)

    assert supervisor.stopped == []
    assert ports.released == []
    async with fake_db.session() as session:  # type: ignore[attr-defined]
        row = await session.get(CodingProject, project.id)
    assert row is not None
    assert row.is_deleted is False


@pytest.mark.asyncio
async def test_delete_owned_project_stops_runtime_and_soft_deletes(
    fake_db: FakeDB, tmp_settings: Settings
) -> None:
    supervisor = RecordingSupervisor()
    ports = RecordingPorts()
    manager = CodingProjectManager(
        tmp_settings,
        fake_db,  # type: ignore[arg-type]
        supervisor=supervisor,  # type: ignore[arg-type]
        ports=ports,  # type: ignore[arg-type]
    )
    owner = uuid4()
    project = await manager.scaffold(
        user_id=owner,
        name="owned",
        template="vanilla-html",
    )

    await manager.delete(project_id=project.id, user_id=owner)

    assert supervisor.stopped == [project.id]
    assert ports.released == [str(project.id)]
    async with fake_db.session() as session:  # type: ignore[attr-defined]
        row = await session.get(CodingProject, project.id)
    assert row is not None
    assert row.is_deleted is True


@pytest.mark.asyncio
async def test_list_and_lookup_match_dashed_sqlite_uuid_rows(
    fake_db: FakeDB,
    tmp_settings: Settings,
    tmp_path: Path,
) -> None:
    manager = CodingProjectManager(tmp_settings, fake_db)  # type: ignore[arg-type]
    user_id = uuid4()
    folder_id = uuid4()
    project_id = uuid4()
    await _insert_dashed_coding_project(
        fake_db,
        project_id=project_id,
        folder_id=folder_id,
        user_id=user_id,
        root_path=tmp_path,
    )

    rows = await manager.list_for_user(user_id)
    by_folder = await manager.get_by_folder_for_user(folder_id, user_id)

    assert [row.id for row in rows] == [project_id]
    assert by_folder.id == project_id
