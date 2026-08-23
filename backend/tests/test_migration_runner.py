"""Migration runner contracts for container startup."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from StAgent.scripts import run_migrations

if TYPE_CHECKING:
    from alembic.config import Config

_REPO_ROOT = Path(__file__).resolve().parents[2]


def test_startup_migration_skip_flag_returns_before_alembic_import(
    monkeypatch: Any,
) -> None:
    import asyncio

    from StAgent import main

    monkeypatch.setattr(main, "_migrations_applied", False)
    monkeypatch.setattr(
        main,
        "get_settings",
        lambda: SimpleNamespace(skip_startup_migrations=True),
    )

    asyncio.run(main._run_db_migrations())

    source = Path(main.__file__).read_text(encoding="utf-8")
    assert main._migrations_applied is True
    assert source.index("if settings.skip_startup_migrations") < source.index(
        "from alembic.script"
    )


def test_sqlite_runner_uses_sync_injected_connection(
    monkeypatch: Any,
) -> None:
    migrated_connections: list[object] = []
    disposed: list[bool] = []
    sync_url = "sqlite:///C:/tmp/StAgent-migration-test/StAgent.db"

    class FakeConnection:
        def __enter__(self) -> FakeConnection:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

    class FakeEngine:
        def __init__(self) -> None:
            self.connection = FakeConnection()

        def connect(self) -> FakeConnection:
            return self.connection

        def dispose(self) -> None:
            disposed.append(True)

    def fake_create_engine(url: str) -> FakeEngine:
        assert url == sync_url
        return FakeEngine()

    def fake_run_sync_migrations(connection: object) -> None:
        migrated_connections.append(connection)

    def fake_settings() -> SimpleNamespace:
        return SimpleNamespace(
            database=SimpleNamespace(sync_url=sync_url),
        )

    monkeypatch.setattr("sqlalchemy.create_engine", fake_create_engine)
    monkeypatch.setattr(
        "StAgent.alembic.sync_runner.run_sync_migrations",
        fake_run_sync_migrations,
    )
    monkeypatch.setattr("StAgent.config.settings.get_settings", fake_settings)

    status = run_migrations._run_sqlite_migrations()

    assert status == 0
    assert len(migrated_connections) == 1
    assert disposed == [True]


def test_postgres_runner_uses_packaged_async_alembic_path(
    monkeypatch: Any,
) -> None:
    calls: list[tuple[Config, str]] = []

    def fake_upgrade(config: Config, revision: str) -> None:
        calls.append((config, revision))

    monkeypatch.setattr("alembic.command.upgrade", fake_upgrade)

    dsn = "postgresql+asyncpg://StAgent:test@127.0.0.1:5432/StAgent"
    status = run_migrations._run_postgres_migrations(dsn)

    assert status == 0
    assert len(calls) == 1
    config, revision = calls[0]
    assert revision == "head"
    assert config.get_main_option("sqlalchemy.url") == dsn


def test_migration_runner_no_longer_imports_psycopg2() -> None:
    source = Path(run_migrations.__file__).read_text(encoding="utf-8")

    assert "import psycopg2" not in source
    assert "psycopg2.connect" not in source


def test_async_alembic_environment_serializes_postgres_migrations() -> None:
    env_source = (_REPO_ROOT / "backend" / "StAgent" / "alembic" / "env.py").read_text(
        encoding="utf-8"
    )

    assert "_configured_database_url()" in env_source
    assert "_async_database_url(configured)" in env_source
    assert "_POSTGRES_ADVISORY_LOCK_KEY" in env_source
    assert "db_settings.is_postgresql" in env_source
    assert "SELECT pg_advisory_lock(:lock_key)" in env_source
    assert "await connection.run_sync(do_run_migrations)" in env_source
    assert "SELECT pg_advisory_unlock(:lock_key)" in env_source


def test_async_alembic_environment_ignores_placeholder_ini_url() -> None:
    env_source = (_REPO_ROOT / "backend" / "StAgent" / "alembic" / "env.py").read_text(
        encoding="utf-8"
    )

    assert 'url.startswith("driver://")' in env_source
