"""Run Alembic migrations once per invocation.

SQLite (default): runs ``alembic upgrade head`` in-process using the packaged
migration environment (works for repo checkouts and ``uv tool install`` wheels).

PostgreSQL (optional): runs the packaged async Alembic path. The environment
uses ``pg_advisory_lock`` so only one replica upgrades at a time; no
``psycopg2`` runtime dependency is required.

Usage::

    python -m StAgent.scripts.run_migrations
"""

from __future__ import annotations

import logging
import sys

logger = logging.getLogger(__name__)

def _run_sqlite_migrations() -> int:
    try:
        from sqlalchemy import create_engine

        from StAgent.alembic.sync_runner import run_sync_migrations
        from StAgent.config.settings import get_settings
    except Exception as exc:  # noqa: BLE001
        logger.error("migration imports failed: %s", exc)
        return 1
    logger.info("running alembic upgrade head (SQLite)")
    settings = get_settings()
    engine = create_engine(settings.database.sync_url)
    try:
        with engine.connect() as connection:
            run_sync_migrations(connection)
    except Exception as exc:  # noqa: BLE001
        logger.error("alembic upgrade failed: %s", exc)
        return 1
    finally:
        engine.dispose()
    logger.info("migrations complete")
    return 0


def _run_postgres_migrations(dsn: str) -> int:
    try:
        from alembic import command

        from StAgent.alembic.runtime_config import load_alembic_config
    except Exception as exc:  # noqa: BLE001
        logger.error("migration imports failed: %s", exc)
        return 1
    logger.info("running alembic upgrade head (PostgreSQL async): %s", dsn)
    try:
        command.upgrade(load_alembic_config(sqlalchemy_url=dsn), "head")
    except Exception as exc:  # noqa: BLE001
        logger.error("alembic upgrade failed: %s", exc)
        return 1
    logger.info("migrations complete")
    return 0


def _run() -> int:
    logging.basicConfig(level=logging.INFO)

    try:
        from StAgent.config.settings import get_settings

        settings = get_settings()
        dsn = settings.database.url
    except Exception:
        logger.error("application settings import failed", exc_info=True)
        return 1

    if dsn.lower().startswith("sqlite"):
        return _run_sqlite_migrations()

    return _run_postgres_migrations(dsn)


def main() -> None:
    sys.exit(_run())


if __name__ == "__main__":
    main()
