"""Alembic environment configuration with async engine support."""

from __future__ import annotations

import asyncio
import contextlib
from logging.config import fileConfig
from typing import TYPE_CHECKING

from alembic import context
from sqlalchemy import text
from sqlmodel import SQLModel

from StAgent.config.settings import get_settings
from StAgent.db.engine import make_async_engine

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Import all models so Alembic can detect them for autogenerate.
# Each model module registers its tables on SQLModel.metadata.
with contextlib.suppress(ImportError):
    from StAgent.db import models as _models  # noqa: F401

target_metadata = SQLModel.metadata

_POSTGRES_ADVISORY_LOCK_KEY = 4242_4242_4242


def _configured_database_url() -> str | None:
    """Return Alembic config's explicit database URL, if supplied."""
    raw = config.get_main_option("sqlalchemy.url") if config is not None else None
    if raw is None:
        return None
    url = raw.strip()
    if not url or url.startswith("driver://"):
        return None
    return url


def _async_database_url(url: str) -> str:
    """Normalize a SQLAlchemy URL to an async-driver URL for Alembic CLI runs."""
    lower = url.lower()
    if lower.startswith("sqlite:///"):
        return f"sqlite+aiosqlite:///{url.split(':///', 1)[1]}"
    if lower.startswith("postgresql://"):
        return f"postgresql+asyncpg://{url.split('://', 1)[1]}"
    if lower.startswith("postgres://"):
        return f"postgresql+asyncpg://{url.split('://', 1)[1]}"
    if lower.startswith("postgresql+psycopg2://"):
        return f"postgresql+asyncpg://{url.split('://', 1)[1]}"
    return url


def _get_url() -> str:
    """Resolve the database URL from application settings."""
    configured = _configured_database_url()
    if configured is not None:
        return configured
    settings = get_settings()
    return settings.database.url


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emit SQL to stdout)."""
    url = _get_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Run migrations using an async engine."""
    settings = get_settings()
    db_settings = settings.database
    configured = _configured_database_url()
    if configured is not None:
        db_settings = db_settings.model_copy(
            update={"database_url": _async_database_url(configured)}
        )
    connectable = make_async_engine(db_settings)

    async with connectable.connect() as connection:
        if db_settings.is_postgresql:
            await connection.execute(
                text("SELECT pg_advisory_lock(:lock_key)"),
                {"lock_key": _POSTGRES_ADVISORY_LOCK_KEY},
            )
        try:
            await connection.run_sync(do_run_migrations)
        finally:
            if db_settings.is_postgresql:
                await connection.execute(
                    text("SELECT pg_advisory_unlock(:lock_key)"),
                    {"lock_key": _POSTGRES_ADVISORY_LOCK_KEY},
                )

    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    When invoked via ``alembic.command.upgrade`` with
    ``config.attributes["connection"]`` set (see ``main._run_db_migrations``),
    reuse that sync connection so ``context.configure`` runs inside a proper
    Alembic migration environment. Otherwise use the async engine path (CLI).
    """
    cfg = context.config
    injected = cfg.attributes.get("connection") if cfg is not None else None
    if injected is not None:
        do_run_migrations(injected)
        return
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
