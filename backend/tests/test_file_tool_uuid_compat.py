"""SQLite dashed/hex UUID compatibility for file service and folder tool paths."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text

from leagent.config.settings import Settings
from leagent.db.service import DatabaseService
from leagent.file.service import FileService
from leagent.tools.base import ToolContext
from leagent.tools.util.folder_tool import FolderOperationsTool


@pytest_asyncio.fixture()
async def file_tool_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    db_path = tmp_path / "file-tool.db"
    monkeypatch.setenv("DB_DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    db = DatabaseService(Settings())
    await db.create_tables()
    try:
        yield db
    finally:
        await db.dispose()


async def _insert_dashed_folder_tool_rows(
    db: DatabaseService,
    *,
    user_id: UUID,
    other_user_id: UUID,
    folder_id: UUID,
    other_folder_id: UUID,
    file_id: UUID,
    unfiled_file_id: UUID,
    other_file_id: UUID,
    storage_dir: Path,
) -> None:
    now = datetime.utcnow().isoformat(sep=" ")
    async with db.session() as session:
        for uid in [user_id, other_user_id]:
            await session.execute(
                text("INSERT INTO users (id, role, disabled) VALUES (:id, 'user', 0)"),
                {"id": str(uid)},
            )
        for fid, uid, name in [
            (folder_id, user_id, "owned folder"),
            (other_folder_id, other_user_id, "other folder"),
        ]:
            await session.execute(
                text(
                    "INSERT INTO folders "
                    "(is_deleted, created_at, updated_at, id, name, user_id, "
                    "parent_id, position, file_count, flow_count, is_project) "
                    "VALUES (0, :now, :now, :id, :name, :uid, NULL, 0, 0, 0, 0)"
                ),
                {"now": now, "id": str(fid), "name": name, "uid": str(uid)},
            )
        for fid, uid, name, folder, text_value in [
            (file_id, user_id, "owned.txt", folder_id, "owned extracted text"),
            (unfiled_file_id, user_id, "unfiled.txt", None, "unfiled text"),
            (other_file_id, other_user_id, "other.txt", other_folder_id, "other text"),
        ]:
            path = storage_dir / name
            path.write_text(text_value, encoding="utf-8")
            await session.execute(
                text(
                    "INSERT INTO files "
                    "(is_deleted, created_at, updated_at, id, name, original_name, "
                    "file_type, size, status, user_id, folder_id, storage_path, "
                    "checksum, extracted_text, has_ocr, is_indexed, library_scope, "
                    "inbox_state, is_pinned) "
                    "VALUES (0, :now, :now, :id, :name, :name, 'document', 4, "
                    "'processed', :uid, :folder_id, :path, :checksum, :text, 0, "
                    "1, 'knowledge', 'new', 0)"
                ),
                {
                    "now": now,
                    "id": str(fid),
                    "name": name,
                    "uid": str(uid),
                    "folder_id": str(folder) if folder is not None else None,
                    "path": str(path),
                    "checksum": f"sha-{fid.hex[:8]}",
                    "text": text_value,
                },
            )


@pytest.mark.asyncio
async def test_file_service_dedup_lookup_matches_dashed_sqlite_user_id(
    file_tool_db: DatabaseService,
    tmp_path: Path,
) -> None:
    user_id = uuid4()
    other_user_id = uuid4()
    folder_id = uuid4()
    other_folder_id = uuid4()
    file_id = uuid4()
    unfiled_file_id = uuid4()
    other_file_id = uuid4()
    await _insert_dashed_folder_tool_rows(
        file_tool_db,
        user_id=user_id,
        other_user_id=other_user_id,
        folder_id=folder_id,
        other_folder_id=other_folder_id,
        file_id=file_id,
        unfiled_file_id=unfiled_file_id,
        other_file_id=other_file_id,
        storage_dir=tmp_path,
    )
    service = FileService(database=file_tool_db)

    path = await service._find_reusable_storage_path(  # noqa: SLF001
        user_id,
        checksum=f"sha-{file_id.hex[:8]}",
        size=4,
    )
    other_path = await service._find_reusable_storage_path(  # noqa: SLF001
        user_id,
        checksum=f"sha-{other_file_id.hex[:8]}",
        size=4,
    )

    assert path == str(tmp_path / "owned.txt")
    assert other_path is None


@pytest.mark.asyncio
async def test_folder_tool_matches_dashed_sqlite_ids_and_enforces_owner(
    file_tool_db: DatabaseService,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    user_id = uuid4()
    other_user_id = uuid4()
    folder_id = uuid4()
    other_folder_id = uuid4()
    file_id = uuid4()
    unfiled_file_id = uuid4()
    other_file_id = uuid4()
    await _insert_dashed_folder_tool_rows(
        file_tool_db,
        user_id=user_id,
        other_user_id=other_user_id,
        folder_id=folder_id,
        other_folder_id=other_folder_id,
        file_id=file_id,
        unfiled_file_id=unfiled_file_id,
        other_file_id=other_file_id,
        storage_dir=tmp_path,
    )

    class _ServiceManager:
        db = file_tool_db
        file_processing = None

    monkeypatch.setattr(
        "leagent.services.service_manager.get_service_manager",
        lambda: _ServiceManager(),
    )
    tool = FolderOperationsTool()
    ctx = ToolContext(user_id=str(user_id), session_id=None)

    folders = await tool.execute({"operation": "list_folders"}, ctx)
    listed = await tool.execute(
        {"operation": "list_files_in_folder", "folder_id": str(folder_id)},
        ctx,
    )
    read_owned = await tool.execute(
        {"operation": "read_folder_file", "file_id": str(file_id)},
        ctx,
    )
    read_foreign = await tool.execute(
        {"operation": "read_folder_file", "file_id": str(other_file_id)},
        ctx,
    )
    add_owned = await tool.execute(
        {
            "operation": "add_file_to_folder",
            "file_id": str(unfiled_file_id),
            "folder_id": str(folder_id),
        },
        ctx,
    )
    add_to_foreign_folder = await tool.execute(
        {
            "operation": "add_file_to_folder",
            "file_id": str(unfiled_file_id),
            "folder_id": str(other_folder_id),
        },
        ctx,
    )
    remove_owned = await tool.execute(
        {"operation": "remove_file_from_folder", "file_id": str(unfiled_file_id)},
        ctx,
    )

    async with file_tool_db.session() as session:
        folder_after_remove = (
            await session.execute(
                text("SELECT folder_id FROM files WHERE id = :id"),
                {"id": str(unfiled_file_id)},
            )
        ).scalar_one_or_none()

    assert [f["id"] for f in folders["folders"]] == [str(folder_id)]
    assert [f["id"] for f in listed["files"]] == [str(file_id)]
    assert read_owned["extracted_text"] == "owned extracted text"
    assert read_foreign["error"] == "Access denied"
    assert add_owned["added"] is True
    assert add_to_foreign_folder["error"] == "Access denied"
    assert remove_owned["removed"] is True
    assert folder_after_remove is None
