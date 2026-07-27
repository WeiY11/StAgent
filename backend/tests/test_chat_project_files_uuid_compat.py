"""SQLite dashed UUID compatibility for chat project file-space helpers."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text

from leagent.config.settings import Settings
from leagent.db.service import DatabaseService
from leagent.services.chat.project_files import (
    link_file_ids_to_folder,
    resolve_session_project_file_space,
)
from leagent.services.chat.projects import ChatProjectService


@pytest_asyncio.fixture()
async def project_files_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    db_path = tmp_path / "project-files.db"
    monkeypatch.setenv("DB_DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    db = DatabaseService(Settings())
    await db.create_tables()
    try:
        yield db
    finally:
        await db.dispose()


async def _insert_dashed_project_session_and_file(
    db: DatabaseService,
    *,
    user_id: UUID,
    project_id: UUID,
    session_id: UUID,
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
                """
                INSERT INTO chat_projects (
                    is_deleted, created_at, updated_at, id, user_id, name
                )
                VALUES (0, :now, :now, :id, :uid, 'Campaign')
                """
            ),
            {"now": now, "id": str(project_id), "uid": str(user_id)},
        )
        await session.execute(
            text(
                """
                INSERT INTO chat_sessions (
                    created_at, updated_at, id, name, user_id, project_id,
                    is_active, message_count
                )
                VALUES (:now, :now, :id, 'session', :uid, :pid, 1, 0)
                """
            ),
            {
                "now": now,
                "id": str(session_id),
                "uid": str(user_id),
                "pid": str(project_id),
            },
        )
        await session.execute(
            text(
                """
                INSERT INTO files (
                    is_deleted, created_at, updated_at, id, name, original_name,
                    file_type, size, status, user_id, storage_path,
                    has_ocr, is_indexed, library_scope, inbox_state, is_pinned
                )
                VALUES (
                    0, :now, :now, :id, 'brief.txt', 'brief.txt',
                    'document', 5, 'processed', :uid, :storage_path,
                    0, 1, 'knowledge', 'new', 0
                )
                """
            ),
            {
                "now": now,
                "id": str(file_id),
                "uid": str(user_id),
                "storage_path": str(storage_path),
            },
        )


async def _insert_dashed_user(db: DatabaseService, user_id: UUID) -> None:
    async with db.session() as session:
        await session.execute(
            text("INSERT INTO users (id, role, disabled) VALUES (:id, 'user', 0)"),
            {"id": str(user_id)},
        )


async def _insert_dashed_project_only(
    db: DatabaseService,
    *,
    user_id: UUID,
    project_id: UUID,
    name: str = "Legacy",
) -> None:
    now = datetime.utcnow().isoformat(sep=" ")
    async with db.session() as session:
        await session.execute(
            text("INSERT INTO users (id, role, disabled) VALUES (:id, 'user', 0)"),
            {"id": str(user_id)},
        )
        await session.execute(
            text(
                """
                INSERT INTO chat_projects (
                    is_deleted, created_at, updated_at, id, user_id, name
                )
                VALUES (0, :now, :now, :id, :uid, :name)
                """
            ),
            {
                "now": now,
                "id": str(project_id),
                "uid": str(user_id),
                "name": name,
            },
        )


@pytest.mark.asyncio
async def test_project_file_space_backfills_and_links_dashed_sqlite_rows(
    project_files_db: DatabaseService,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from leagent.services.chat import project_files as project_files_mod

    monkeypatch.setattr(project_files_mod, "WORKING_DIR", tmp_path / "working")
    user_id = uuid4()
    project_id = uuid4()
    session_id = uuid4()
    file_id = uuid4()
    storage_path = tmp_path / "brief.txt"
    storage_path.write_text("brief", encoding="utf-8")
    await _insert_dashed_project_session_and_file(
        project_files_db,
        user_id=user_id,
        project_id=project_id,
        session_id=session_id,
        file_id=file_id,
        storage_path=storage_path,
    )

    space = await resolve_session_project_file_space(
        project_files_db,
        session_id=session_id,
        user_id=user_id,
    )
    assert space is not None
    linked = await link_file_ids_to_folder(
        project_files_db,
        file_ids=[file_id],
        folder_id=space.folder_id,
        user_id=user_id,
    )

    async with project_files_db.session() as session:
        project_row = (
            await session.execute(
                text(
                    """
                    SELECT CAST(folder_id AS TEXT) AS folder_id
                    FROM chat_projects
                    WHERE id = :project_id
                    """
                ),
                {"project_id": str(project_id)},
            )
        ).mappings().one()
        folder_rows = (
            await session.execute(
                text(
                    """
                    SELECT CAST(id AS TEXT) AS id,
                           CAST(user_id AS TEXT) AS user_id,
                           file_count
                    FROM folders
                    ORDER BY created_at ASC
                    """
                )
            )
        ).mappings().all()
        file_folder = (
            await session.execute(
                text(
                    """
                    SELECT CAST(folder_id AS TEXT)
                    FROM files
                    WHERE id = :file_id
                    """
                ),
                {"file_id": str(file_id)},
            )
        ).scalar_one()

    assert linked == 1
    assert project_row["folder_id"] == space.folder_id.hex
    assert {row["user_id"] for row in folder_rows} == {str(user_id)}
    assert any(row["id"] == space.folder_id.hex and row["file_count"] == 1 for row in folder_rows)
    assert file_folder == space.folder_id.hex


@pytest.mark.asyncio
async def test_chat_project_service_create_update_delete_preserves_dashed_user_fks(
    project_files_db: DatabaseService,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from leagent.services.chat import project_files as project_files_mod

    monkeypatch.setattr(project_files_mod, "WORKING_DIR", tmp_path / "working")
    user_id = uuid4()
    session_id = uuid4()
    await _insert_dashed_user(project_files_db, user_id)
    service = ChatProjectService(project_files_db)

    project = await service.create_project(user_id=user_id, name="Old name")
    updated = await service.update_project(project.id, user_id=user_id, name="New name")
    assert updated is not None

    now = datetime.utcnow().isoformat(sep=" ")
    async with project_files_db.session() as session:
        await session.execute(
            text(
                """
                INSERT INTO chat_sessions (
                    created_at, updated_at, id, name, user_id, project_id,
                    is_active, message_count
                )
                VALUES (:now, :now, :id, 'session', :uid, :pid, 1, 0)
                """
            ),
            {
                "now": now,
                "id": str(session_id),
                "uid": str(user_id),
                "pid": project.id.hex,
            },
        )

    listed = await service.list_projects(user_id)
    deleted = await service.delete_project(project.id, user_id=user_id)

    async with project_files_db.session() as session:
        project_row = (
            await session.execute(
                text(
                    """
                    SELECT CAST(user_id AS TEXT) AS user_id,
                           CAST(folder_id AS TEXT) AS folder_id,
                           name,
                           is_deleted
                    FROM chat_projects
                    WHERE id = :project_id
                    """
                ),
                {"project_id": project.id.hex},
            )
        ).mappings().one()
        folder_row = (
            await session.execute(
                text(
                    """
                    SELECT CAST(user_id AS TEXT) AS user_id,
                           name,
                           is_deleted
                    FROM folders
                    WHERE id = :folder_id
                    """
                ),
                {"folder_id": project_row["folder_id"]},
            )
        ).mappings().one()
        session_project_id = (
            await session.execute(
                text(
                    """
                    SELECT CAST(project_id AS TEXT)
                    FROM chat_sessions
                    WHERE id = :session_id
                    """
                ),
                {"session_id": str(session_id)},
            )
        ).scalar_one()

    assert [row.id for row in listed] == [project.id]
    assert deleted is True
    assert project_row["user_id"] == str(user_id)
    assert project_row["name"] == "New name"
    assert bool(project_row["is_deleted"]) is True
    assert folder_row["user_id"] == str(user_id)
    assert folder_row["name"] == "New name"
    assert bool(folder_row["is_deleted"]) is True
    assert session_project_id is None


@pytest.mark.asyncio
async def test_chat_project_service_updates_legacy_dashed_project_primary_key(
    project_files_db: DatabaseService,
) -> None:
    user_id = uuid4()
    project_id = uuid4()
    await _insert_dashed_project_only(
        project_files_db,
        user_id=user_id,
        project_id=project_id,
    )
    service = ChatProjectService(project_files_db)

    updated = await service.update_project(
        project_id,
        user_id=user_id,
        name="Legacy updated",
        clear_password=True,
    )
    deleted = await service.delete_project(project_id, user_id=user_id)

    async with project_files_db.session() as session:
        row = (
            await session.execute(
                text(
                    """
                    SELECT name, is_deleted
                    FROM chat_projects
                    WHERE id = :project_id
                    """
                ),
                {"project_id": str(project_id)},
            )
        ).mappings().one()

    assert updated is not None
    assert updated.name == "Legacy updated"
    assert deleted is True
    assert row["name"] == "Legacy updated"
    assert bool(row["is_deleted"]) is True
