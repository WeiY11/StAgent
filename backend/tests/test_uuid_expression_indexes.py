"""Expression-index coverage for the SQLite UUID-compat predicates.

``uuid_column_equals`` / ``sqlite_parent_id_text`` wrap UUID columns in
``lower(replace(CAST(col AS VARCHAR), '-', ''))``; without a matching
expression index every filtered query degrades to a full table scan. These
tests pin both the index existence and the planner actually choosing them.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from sqlalchemy import text

from StAgent.config.settings import Settings
from StAgent.db.service import DatabaseService


@pytest.fixture()
async def db(monkeypatch: pytest.MonkeyPatch):
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "expridx.db"
        monkeypatch.setenv("DB_DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
        service = DatabaseService(Settings())
        await service.create_tables()
        yield service
        await service.dispose()


async def _explain(db: DatabaseService, sql: str) -> str:
    async with db.session() as session:
        result = await session.execute(text("EXPLAIN QUERY PLAN " + sql))
        rows = result.fetchall()
    return " | ".join(str(r) for r in rows)


async def test_expression_indexes_created_on_bootstrap(db: DatabaseService) -> None:
    async with db.session() as session:
        result = await session.execute(
            text(
                "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'ixu_%'"
            )
        )
        names = {row[0] for row in result.fetchall()}
    # PK twins + the composite hot paths.
    assert "ixu_chat_sessions_id" in names
    assert "ixu_messages_id" in names
    assert "ixu_files_user_id" in names
    # Composite override replaces the single-column twin for these columns.
    assert "ixu_messages_session_id_created_at" in names
    assert "ixu_chat_sessions_user_id_updated_at" in names
    assert "ixu_messages_session_id" not in names


async def test_uuid_column_predicate_uses_index(db: DatabaseService) -> None:
    plan = await _explain(
        db,
        "SELECT id FROM messages "
        "WHERE lower(replace(CAST(session_id AS VARCHAR), '-', '')) = 'abc'",
    )
    assert "USING INDEX ixu_messages_session_id_created_at" in plan


async def test_uuid_in_predicate_uses_index(db: DatabaseService) -> None:
    plan = await _explain(
        db,
        "SELECT id FROM files "
        "WHERE lower(replace(CAST(user_id AS VARCHAR), '-', '')) IN ('abc', 'def')",
    )
    assert "USING INDEX ixu_files_user_id" in plan


async def test_parent_id_lookup_uses_index(db: DatabaseService) -> None:
    plan = await _explain(
        db,
        "SELECT CAST(id AS TEXT) FROM chat_sessions "
        "WHERE lower(replace(CAST(id AS VARCHAR), '-', '')) = 'abc'",
    )
    assert "USING INDEX ixu_chat_sessions_id" in plan


async def test_parent_id_text_matches_dashed_and_hex_rows(
    db: DatabaseService,
) -> None:
    """Regression: the simplified single-condition parent lookup must still
    find rows stored in either dashed or hex text form."""
    from StAgent.db.sqlite_compat import sqlite_parent_id_text

    dashed_uuid = "12345678-1234-5678-1234-567812345678"
    hex_uuid = "87654321-4321-8765-4321-876543218765".replace("-", "")
    # create_tables() seeded the identity stub; its id is stored as 32-char
    # hex, and the raw FK check compares stored text verbatim.
    owner = "00000000000000000000000000000001"
    async with db.session() as session:
        for sid in (dashed_uuid, hex_uuid):
            await session.execute(
                text(
                    "INSERT INTO chat_sessions "
                    "(created_at, updated_at, id, user_id, is_active, message_count) "
                    "VALUES ('2026-01-01', '2026-01-01', :id, :owner, 1, 0)"
                ),
                {"id": sid, "owner": owner},
            )

    import uuid as uuid_mod

    async with db.session() as session:
        dashed_found = await sqlite_parent_id_text(
            session, "chat_sessions", uuid_mod.UUID(dashed_uuid)
        )
        hex_found = await sqlite_parent_id_text(
            session, "chat_sessions", uuid_mod.UUID(hex_uuid)
        )
    assert dashed_found == dashed_uuid
    assert hex_found == hex_uuid
