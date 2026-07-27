"""Real PostgreSQL startup-order smoke test.

Skipped unless explicitly run as a live test with ``DATABASE_URL`` pointing at a
reachable PostgreSQL instance. The test mirrors the app startup contract:
SQLModel creates the base schema first, then Alembic applies additive
migrations, then representative enum-backed rows round-trip through asyncpg.
"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlmodel.ext.asyncio.session import AsyncSession

import leagent.db.models  # noqa: F401
from leagent.config.settings import DatabaseSettings, Settings
from leagent.db.engine import make_async_engine
from leagent.db.models.coding_project import (
    CodingProject,
    CodingProjectRuntimeKind,
    CodingProjectStatus,
)
from leagent.db.models.file import File, FileStatus, FileType, InboxState, LibraryScope
from leagent.db.models.flow import Flow, FlowStatus, FlowType
from leagent.db.models.message import Message, MessageRole, MessageStatus
from leagent.db.models.task import Task, TaskPriority, TaskStatus, TaskType
from leagent.db.service import DatabaseService

_DATABASE_URL = os.environ.get("DATABASE_URL", "")
pytestmark = [pytest.mark.integration, pytest.mark.live]


@pytest.mark.skipif(
    "postgres" not in _DATABASE_URL,
    reason="requires DATABASE_URL pointing at a PostgreSQL instance",
)
def test_postgres_bootstrap_migrations_and_roundtrips() -> None:
    import asyncio

    from alembic import command
    from alembic.script import ScriptDirectory

    from leagent.alembic.runtime_config import load_alembic_config

    db_settings = DatabaseSettings(database_url=_DATABASE_URL)
    app_settings = Settings(
        database=db_settings,
        secret_key=os.environ.get(
            "SECRET_KEY",
            "test-secret-key-with-at-least-32-bytes",
        ),
    )
    assert db_settings.backend == "postgresql"

    asyncio.run(_create_base_schema(app_settings))

    config = load_alembic_config(sqlalchemy_url=_DATABASE_URL)
    head = ScriptDirectory.from_config(config).get_current_head()
    assert head is not None
    command.upgrade(config, "head")

    asyncio.run(_assert_head_and_roundtrip(db_settings, head))


async def _create_base_schema(settings: Settings) -> None:
    service = DatabaseService(settings)
    try:
        await service.create_tables()
    finally:
        await service.dispose()


async def _assert_head_and_roundtrip(
    settings: DatabaseSettings,
    expected_head: str,
) -> None:
    engine = make_async_engine(settings)
    try:
        async with engine.begin() as connection:
            assert await connection.scalar(text("select 1")) == 1
            assert (
                await connection.scalar(text("SELECT version_num FROM alembic_version"))
                == expected_head
            )

        marker = uuid4().hex
        session_factory = async_sessionmaker(
            engine, class_=AsyncSession, expire_on_commit=False
        )
        async with session_factory() as session:
            session.add(
                File(
                    name=f"{marker}.pdf",
                    original_name=f"{marker}.pdf",
                    file_type=FileType.DOCUMENT,
                    status=FileStatus.PROCESSED,
                    storage_path=f"/tmp/{marker}.pdf",
                    library_scope=LibraryScope.KNOWLEDGE,
                    inbox_state=InboxState.TRIAGED,
                )
            )
            session.add(
                Message(
                    session_id=uuid4(),
                    role=MessageRole.ASSISTANT,
                    status=MessageStatus.COMPLETED,
                    content="enum smoke",
                )
            )
            session.add(
                Task(
                    name=f"enum-smoke-{marker}",
                    task_type=TaskType.WORKFLOW,
                    status=TaskStatus.RUNNING,
                    priority=TaskPriority.HIGH,
                )
            )
            session.add(
                Flow(
                    name=f"enum-smoke-{marker}",
                    flow_type=FlowType.WORKFLOW,
                    status=FlowStatus.PUBLISHED,
                )
            )
            session.add(
                CodingProject(
                    name=f"enum-smoke-{marker}",
                    template="fastapi",
                    runtime_kind=CodingProjectRuntimeKind.FASTAPI,
                    root_path=f"/tmp/leagent-{marker}",
                    status=CodingProjectStatus.RUNNING,
                )
            )
            await session.commit()

        async with engine.connect() as connection:
            file_row = (
                await connection.execute(
                    text(
                        "SELECT file_type, status, library_scope, inbox_state "
                        "FROM files WHERE original_name = :name"
                    ),
                    {"name": f"{marker}.pdf"},
                )
            ).one()
            message_row = (
                await connection.execute(
                    text(
                        "SELECT role, status FROM messages "
                        "WHERE content = 'enum smoke' ORDER BY created_at DESC LIMIT 1"
                    )
                )
            ).one()
            task_row = (
                await connection.execute(
                    text(
                        "SELECT task_type, status, priority FROM tasks "
                        "WHERE name = :name"
                    ),
                    {"name": f"enum-smoke-{marker}"},
                )
            ).one()
            flow_row = (
                await connection.execute(
                    text(
                        "SELECT status, flow_type FROM flows "
                        "WHERE name = :name"
                    ),
                    {"name": f"enum-smoke-{marker}"},
                )
            ).one()
            project_row = (
                await connection.execute(
                    text(
                        "SELECT runtime_kind, status FROM coding_projects "
                        "WHERE name = :name"
                    ),
                    {"name": f"enum-smoke-{marker}"},
                )
            ).one()

        assert tuple(file_row) == ("document", "processed", "knowledge", "triaged")
        assert tuple(message_row) == ("assistant", "completed")
        assert tuple(task_row) == ("workflow", "running", "high")
        assert tuple(flow_row) == ("published", "workflow")
        assert tuple(project_row) == ("fastapi", "running")
    finally:
        await engine.dispose()
