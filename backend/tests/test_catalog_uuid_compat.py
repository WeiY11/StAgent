"""SQLite dashed/hex UUID compatibility for catalog and folder queries."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text

from StAgent.api.v1.documents import list_documents
from StAgent.api.v1.files import search_files
from StAgent.api.v1.folder_items import list_folder_items
from StAgent.api.v1.folders import delete_folder, get_folder_tree, list_folders
from StAgent.config.settings import Settings
from StAgent.db.service import DatabaseService


@pytest_asyncio.fixture()
async def catalog_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    db_path = tmp_path / "catalog.db"
    monkeypatch.setenv("DB_DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    db = DatabaseService(Settings())
    await db.create_tables()
    try:
        yield db
    finally:
        await db.dispose()


async def _insert_dashed_catalog_rows(
    db: DatabaseService,
    *,
    user_id: UUID,
    parent_id: UUID,
    child_id: UUID,
    file_id: UUID,
    storage_path: Path,
) -> None:
    now = datetime.utcnow().isoformat(sep=" ")
    async with db.session() as session:
        await session.execute(
            text("INSERT INTO users (id, role, disabled) VALUES (:id, 'user', 0)"),
            {"id": str(user_id)},
        )
        await session.execute(
            text(
                "INSERT INTO folders "
                "(is_deleted, created_at, updated_at, id, name, user_id, parent_id, "
                "position, file_count, flow_count, is_project) "
                "VALUES (0, :now, :now, :id, 'parent', :uid, NULL, 0, 0, 0, 0)"
            ),
            {"now": now, "id": str(parent_id), "uid": str(user_id)},
        )
        await session.execute(
            text(
                "INSERT INTO folders "
                "(is_deleted, created_at, updated_at, id, name, user_id, parent_id, "
                "position, file_count, flow_count, is_project) "
                "VALUES (0, :now, :now, :id, 'child', :uid, :parent, 1, 1, 0, 0)"
            ),
            {
                "now": now,
                "id": str(child_id),
                "uid": str(user_id),
                "parent": str(parent_id),
            },
        )
        await session.execute(
            text(
                "INSERT INTO files "
                "(is_deleted, created_at, updated_at, id, name, original_name, file_type, "
                "size, status, user_id, folder_id, storage_path, extracted_text, has_ocr, "
                "is_indexed, library_scope, inbox_state, is_pinned) "
                "VALUES (0, :now, :now, :id, 'catalog-note.txt', 'catalog-note.txt', "
                "'document', 4, 'processed', :uid, :folder, :path, 'catalog text', "
                "0, 1, 'knowledge', 'new', 0)"
            ),
            {
                "now": now,
                "id": str(file_id),
                "uid": str(user_id),
                "folder": str(child_id),
                "path": str(storage_path),
            },
        )


@pytest.mark.asyncio
async def test_catalog_and_folder_queries_match_dashed_sqlite_uuid_rows(
    catalog_db: DatabaseService,
    tmp_path: Path,
) -> None:
    user_id = uuid4()
    parent_id = uuid4()
    child_id = uuid4()
    file_id = uuid4()
    storage_path = tmp_path / "catalog-note.txt"
    storage_path.write_text("catalog", encoding="utf-8")
    await _insert_dashed_catalog_rows(
        catalog_db,
        user_id=user_id,
        parent_id=parent_id,
        child_id=child_id,
        file_id=file_id,
        storage_path=storage_path,
    )

    root_folders = await list_folders(
        user_id,
        catalog_db,
        page=1,
        page_size=10,
        parent_id=None,
    )
    child_folders = await list_folders(
        user_id,
        catalog_db,
        page=1,
        page_size=10,
        parent_id=parent_id,
    )
    tree = await get_folder_tree(user_id, catalog_db)
    folder_items = await list_folder_items(child_id, user_id, catalog_db)
    docs = await list_documents(
        user_id,
        catalog_db,
        page=1,
        page_size=10,
        file_type=None,
        status=None,
        folder_id=child_id,
        unfiled=False,
        search=None,
    )
    file_search = await search_files(
        user_id,
        catalog_db,
        query="catalog-note",
        library_scope=None,
        limit=10,
    )

    assert [f.id for f in root_folders.items] == [parent_id]
    assert [f.id for f in child_folders.items] == [child_id]
    assert [n.id for n in tree] == [parent_id]
    assert [n.id for n in tree[0].children] == [child_id]
    assert [item.file_id for item in folder_items] == [file_id]
    assert [doc.id for doc in docs.items] == [file_id]
    assert [hit.file_id for hit in file_search.results] == [file_id]


@pytest.mark.asyncio
async def test_recursive_delete_matches_dashed_sqlite_uuid_children(
    catalog_db: DatabaseService,
    tmp_path: Path,
) -> None:
    user_id = uuid4()
    parent_id = uuid4()
    child_id = uuid4()
    file_id = uuid4()
    storage_path = tmp_path / "catalog-note.txt"
    storage_path.write_text("catalog", encoding="utf-8")
    await _insert_dashed_catalog_rows(
        catalog_db,
        user_id=user_id,
        parent_id=parent_id,
        child_id=child_id,
        file_id=file_id,
        storage_path=storage_path,
    )

    await delete_folder(parent_id, user_id, catalog_db, recursive=True)

    async with catalog_db.session() as session:
        parent = (
            await session.execute(
                text("SELECT is_deleted FROM folders WHERE id = :id"),
                {"id": str(parent_id)},
            )
        ).scalar_one()
        child = (
            await session.execute(
                text("SELECT is_deleted FROM folders WHERE id = :id"),
                {"id": str(child_id)},
            )
        ).scalar_one()

    assert bool(parent) is True
    assert bool(child) is True
