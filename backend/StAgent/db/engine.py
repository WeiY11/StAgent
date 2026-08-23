"""Async engine construction for the persistence layer.

Single source of truth for building the SQLAlchemy async engine. SQLite uses
WAL mode with a small pooled engine (every repository method opens its own
session; pooling the underlying aiosqlite connections avoids re-paying
connection setup + the six pragmas per call). PostgreSQL uses a pre-pinged
connection pool. Both ``DatabaseService`` and the Alembic environment call
:func:`make_async_engine` so engine configuration is never duplicated.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

if TYPE_CHECKING:
    from StAgent.config.settings import DatabaseSettings

logger = logging.getLogger(__name__)


def _sqlite_connect_pragma(dbapi_conn: object, _: object) -> None:
    """Apply SQLite pragmas on each new DBAPI connection."""
    cursor = dbapi_conn.cursor()  # type: ignore[attr-defined]
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.execute("PRAGMA cache_size=-64000")  # 64MB page cache
    cursor.execute("PRAGMA mmap_size=268435456")  # 256MB memory-mapped I/O
    cursor.close()


def make_async_engine(db_cfg: DatabaseSettings) -> AsyncEngine:
    """Build the async engine for *db_cfg*.

    Args:
        db_cfg: The ``DatabaseSettings`` describing the target backend.

    Returns:
        A configured :class:`~sqlalchemy.ext.asyncio.AsyncEngine`.
    """
    url = db_cfg.url

    if db_cfg.is_postgresql:
        engine = create_async_engine(
            url,
            echo=db_cfg.echo,
            pool_size=db_cfg.pool_size,
            max_overflow=db_cfg.max_overflow,
            pool_pre_ping=True,
        )
        logger.info("Async engine initialized (PostgreSQL)")
        return engine

    Path(db_cfg._sqlite_file_path()).parent.mkdir(parents=True, exist_ok=True)
    # :memory: keeps SQLAlchemy's StaticPool default (one shared connection,
    # otherwise every pooled handle would see its own empty database) — and
    # StaticPool rejects queue-pool arguments outright.
    is_memory = db_cfg.url.endswith("/:memory:") or db_cfg.url == "sqlite+aiosqlite://"
    pool_kwargs: dict[str, Any] = (
        {}
        if is_memory
        else {
            # Pooled connections: repositories are method-scoped, so a
            # NullPool here meant a fresh aiosqlite connection (worker
            # thread + pragmas) per repository call. WAL permits concurrent
            # readers and busy_timeout arbitrates write contention;
            # pre_ping keeps handles alive across test-suite file churn.
            "pool_size": db_cfg.pool_size,
            "max_overflow": db_cfg.max_overflow,
            "pool_pre_ping": True,
        }
    )
    engine = create_async_engine(
        url,
        echo=db_cfg.echo,
        connect_args={"check_same_thread": False},
        **pool_kwargs,
    )
    event.listen(engine.sync_engine, "connect", _sqlite_connect_pragma)
    logger.info("Async engine initialized (SQLite WAL: %s)", db_cfg._sqlite_file_path())
    return engine
