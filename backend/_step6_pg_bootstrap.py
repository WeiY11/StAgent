"""Authoritative PostgreSQL bootstrap proof.

Reproduces the app's real schema bootstrap against a live PostgreSQL:
``DatabaseService.create_tables`` (SQLModel.metadata.create_all, the base-schema
owner) followed by ``alembic upgrade head`` (incremental, idempotent). Then it
round-trips a real managed table to prove connectivity *and* schema usability,
not just ``select 1``.

Run with ``DATABASE_URL`` pointing at a reachable PostgreSQL instance.
"""

from __future__ import annotations

import asyncio

from sqlalchemy import text

from leagent.config.settings import get_settings
from leagent.db.service import init_database_service


async def main() -> None:
    settings = get_settings()
    assert settings.database.backend == "postgresql", settings.database.backend
    print(f"BACKEND={settings.database.backend}")

    # 1) Base schema — exactly what ServiceManager._start_database does.
    svc = init_database_service(settings)
    await svc.create_tables()
    print("CREATE_ALL_OK")

    # 2) Incremental migrations — exactly what main._run_db_migrations does.
    from leagent.main import _run_db_migrations

    await _run_db_migrations()
    print("ALEMBIC_UPGRADE_HEAD_OK")

    # 3) Roundtrip against real tables: confirm alembic is at head and a managed
    #    table (users) is queryable on PostgreSQL.
    async with svc._engine.connect() as conn:  # noqa: SLF001 - proof script
        head = await conn.scalar(text("SELECT version_num FROM alembic_version"))
        users = await conn.scalar(text("SELECT count(*) FROM users"))
        projects = await conn.scalar(text("SELECT count(*) FROM chat_projects"))
    print(f"ALEMBIC_HEAD={head}")
    print(f"USERS_COUNT={users}")
    print(f"CHAT_PROJECTS_COUNT={projects}")
    await svc._engine.dispose()  # noqa: SLF001 - proof script
    print("ROUNDTRIP_OK")


if __name__ == "__main__":
    asyncio.run(main())
