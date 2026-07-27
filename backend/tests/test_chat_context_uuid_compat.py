"""SQLite UUID-shape regression tests for chat context resolution."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text

from leagent.api.v1.chat.context_sources import (
    resolve_folder_context,
    resolve_folder_context_note,
    resolve_knowledge_message_paths,
)
from leagent.config.settings import Settings
from leagent.db.service import DatabaseService


@pytest_asyncio.fixture()
async def context_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    db_path = tmp_path / "chat-context.db"
    monkeypatch.setenv("DB_DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    db = DatabaseService(Settings())
    await db.create_tables()
    try:
        yield db
    finally:
        await db.dispose()


async def _insert_dashed_user_folder_file(
    db: DatabaseService,
    *,
    user_id: UUID,
    folder_id: UUID,
    file_id: UUID,
    storage_path: Path,
    original_name: str = "ctx.txt",
    parent_id: UUID | None = None,
) -> None:
    now = datetime.utcnow().isoformat(sep=" ")
    async with db.session() as session:
        await session.execute(
            text(
                "INSERT INTO users (id, role, disabled) "
                "VALUES (:id, 'user', 0)"
            ),
            {"id": str(user_id)},
        )
        if parent_id is not None:
            await session.execute(
                text(
                    "INSERT INTO folders "
                    "(is_deleted, created_at, updated_at, id, name, user_id, parent_id, "
                    "position, file_count, flow_count, is_project) "
                    "VALUES (0, :now, :now, :id, :name, :uid, NULL, 0, 0, 0, 0)"
                ),
                {
                    "now": now,
                    "id": str(parent_id),
                    "name": "parent",
                    "uid": str(user_id),
                },
            )
        await session.execute(
            text(
                "INSERT INTO folders "
                "(is_deleted, created_at, updated_at, id, name, user_id, parent_id, "
                "position, file_count, flow_count, is_project) "
                "VALUES (0, :now, :now, :id, :name, :uid, :parent_id, 0, 1, 0, 0)"
            ),
            {
                "now": now,
                "id": str(folder_id),
                "name": "child",
                "uid": str(user_id),
                "parent_id": str(parent_id) if parent_id is not None else None,
            },
        )
        await session.execute(
            text(
                "INSERT INTO files "
                "(is_deleted, created_at, updated_at, id, name, original_name, file_type, "
                "size, status, user_id, folder_id, storage_path, extracted_text, has_ocr, "
                "is_indexed, library_scope, inbox_state, is_pinned) "
                "VALUES (0, :now, :now, :id, :name, :name, 'document', 4, "
                "'processed', :uid, :folder_id, :storage_path, 'preview text', "
                "0, 1, 'knowledge', 'new', 0)"
            ),
            {
                "now": now,
                "id": str(file_id),
                "name": original_name,
                "uid": str(user_id),
                "folder_id": str(folder_id),
                "storage_path": str(storage_path),
            },
        )


@pytest.mark.asyncio
async def test_resolve_folder_context_matches_dashed_sqlite_uuid_rows(
    context_db: DatabaseService,
    tmp_path: Path,
) -> None:
    user_id = uuid4()
    folder_id = uuid4()
    file_id = uuid4()
    storage_path = tmp_path / "ctx.txt"
    storage_path.write_text("ctx", encoding="utf-8")
    await _insert_dashed_user_folder_file(
        context_db,
        user_id=user_id,
        folder_id=folder_id,
        file_id=file_id,
        storage_path=storage_path,
    )

    by_folder = await resolve_folder_context(
        user_id,
        context_db,
        folder_id=str(folder_id),
    )
    by_file_id = await resolve_folder_context(
        user_id,
        context_db,
        file_ids_csv=str(file_id),
    )

    assert [Path(row[0]) for row in by_folder] == [storage_path]
    assert [Path(row[0]) for row in by_file_id] == [storage_path]


@pytest.mark.asyncio
async def test_knowledge_refs_and_folder_note_match_dashed_sqlite_uuid_rows(
    context_db: DatabaseService,
    tmp_path: Path,
) -> None:
    user_id = uuid4()
    parent_id = uuid4()
    folder_id = uuid4()
    file_id = uuid4()
    storage_path = tmp_path / "knowledge.txt"
    storage_path.write_text("ctx", encoding="utf-8")
    await _insert_dashed_user_folder_file(
        context_db,
        user_id=user_id,
        folder_id=folder_id,
        file_id=file_id,
        storage_path=storage_path,
        original_name="knowledge.txt",
        parent_id=parent_id,
    )

    by_id = await resolve_knowledge_message_paths(
        user_id,
        context_db,
        f"read @knowledge:knowledge.txt#{file_id}",
    )
    by_name = await resolve_knowledge_message_paths(
        user_id,
        context_db,
        "read @knowledge:knowledge.txt",
    )
    note = await resolve_folder_context_note(
        user_id,
        context_db,
        str(folder_id),
        attached_file_count=1,
    )

    assert [Path(p) for p in by_id] == [storage_path.resolve()]
    assert [Path(p) for p in by_name] == [storage_path.resolve()]
    assert note is not None
    assert "parent / child" in note
