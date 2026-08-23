"""Regression tests for folder ownership and folder-item consistency."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi import HTTPException
from starlette.datastructures import UploadFile

from StAgent.api.v1 import documents as v1_documents
from StAgent.api.v1 import files as v1_files
from StAgent.api.v1.folder_items import AddItemRequest, add_item_to_folder
from StAgent.api.v1.folders import FolderUpdateRequest, delete_folder, update_folder
from StAgent.config.settings import Settings
from StAgent.db.models.file import File, FileStatus, FileType, LibraryScope
from StAgent.db.models.folder import Folder
from StAgent.db.models.identity_stub import UserStub
from StAgent.db.service import DatabaseService


@pytest_asyncio.fixture()
async def folder_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    db_path = tmp_path / "folder-test.db"
    monkeypatch.setenv("DB_DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    db = DatabaseService(Settings())
    await db.create_tables()
    try:
        yield db
    finally:
        await db.dispose()


async def _add_user(db: DatabaseService, user_id: UUID) -> None:
    async with db.session() as session:
        if await session.get(UserStub, user_id) is None:
            session.add(UserStub(id=user_id))


async def _add_folder(
    db: DatabaseService,
    *,
    folder_id: UUID,
    user_id: UUID,
    name: str,
    parent_id: UUID | None = None,
    file_count: int = 0,
) -> Folder:
    async with db.session() as session:
        folder = Folder(
            id=folder_id,
            user_id=user_id,
            name=name,
            parent_id=parent_id,
            file_count=file_count,
        )
        session.add(folder)
        await session.flush()
        await session.refresh(folder)
        return folder


async def _add_file(
    db: DatabaseService,
    *,
    file_id: UUID,
    user_id: UUID,
    folder_id: UUID | None,
) -> File:
    async with db.session() as session:
        row = File(
            id=file_id,
            user_id=user_id,
            name="note.txt",
            original_name="note.txt",
            file_type=FileType.DOCUMENT,
            size=4,
            status=FileStatus.PROCESSED,
            storage_path="/tmp/note.txt",
            folder_id=folder_id,
            library_scope=LibraryScope.WORKSPACE,
        )
        session.add(row)
        await session.flush()
        await session.refresh(row)
        return row


def _upload(name: str = "note.txt") -> UploadFile:
    return UploadFile(filename=name, file=BytesIO(b"note"))


@pytest.mark.asyncio
async def test_file_upload_rejects_foreign_folder_before_storing_blob(
    folder_db: DatabaseService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = uuid4()
    other = uuid4()
    foreign_folder_id = uuid4()
    await _add_user(folder_db, owner)
    await _add_user(folder_db, other)
    await _add_folder(
        folder_db,
        folder_id=foreign_folder_id,
        user_id=other,
        name="other",
    )

    called = False

    async def fake_store_blob(*_args: object, **_kwargs: object) -> object:
        nonlocal called
        called = True
        raise AssertionError("blob storage should not run for a foreign folder")

    monkeypatch.setattr(v1_files, "_store_blob", fake_store_blob)

    with pytest.raises(ValueError, match="Folder not found or access denied"):
        await v1_files.persist_uploaded_file(
            _upload(),
            owner,
            folder_db,
            folder_id=foreign_folder_id,
        )

    assert called is False


@pytest.mark.asyncio
async def test_document_upload_rejects_foreign_folder_before_storing_blob(
    folder_db: DatabaseService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = uuid4()
    other = uuid4()
    foreign_folder_id = uuid4()
    await _add_user(folder_db, owner)
    await _add_user(folder_db, other)
    await _add_folder(
        folder_db,
        folder_id=foreign_folder_id,
        user_id=other,
        name="other",
    )

    called = False

    async def fake_store_blob(*_args: object, **_kwargs: object) -> object:
        nonlocal called
        called = True
        raise AssertionError("blob storage should not run for a foreign folder")

    monkeypatch.setattr(v1_files, "_store_blob", fake_store_blob)

    with pytest.raises(HTTPException) as exc:
        await v1_documents.upload_document(
            owner,
            folder_db,
            _upload(),
            folder_id=foreign_folder_id,
        )

    assert exc.value.status_code == 400
    assert called is False


@pytest.mark.asyncio
async def test_recursive_delete_does_not_delete_foreign_child_folder(
    folder_db: DatabaseService,
) -> None:
    owner = uuid4()
    other = uuid4()
    parent_id = uuid4()
    foreign_child_id = uuid4()
    await _add_user(folder_db, owner)
    await _add_user(folder_db, other)
    await _add_folder(folder_db, folder_id=parent_id, user_id=owner, name="owner")
    await _add_folder(
        folder_db,
        folder_id=foreign_child_id,
        user_id=other,
        name="foreign-child",
        parent_id=parent_id,
    )

    await delete_folder(parent_id, owner, folder_db, recursive=True)

    async with folder_db.session() as session:
        parent = await session.get(Folder, parent_id)
        child = await session.get(Folder, foreign_child_id)

    assert parent is not None and parent.is_deleted is True
    assert child is not None and child.is_deleted is False


@pytest.mark.asyncio
async def test_update_folder_rejects_descendant_as_new_parent(
    folder_db: DatabaseService,
) -> None:
    owner = uuid4()
    parent_id = uuid4()
    child_id = uuid4()
    await _add_user(folder_db, owner)
    await _add_folder(folder_db, folder_id=parent_id, user_id=owner, name="parent")
    await _add_folder(
        folder_db,
        folder_id=child_id,
        user_id=owner,
        name="child",
        parent_id=parent_id,
    )

    with pytest.raises(HTTPException) as exc:
        await update_folder(
            parent_id,
            FolderUpdateRequest(parent_id=child_id),
            owner,
            folder_db,
        )

    assert exc.value.status_code == 400
    async with folder_db.session() as session:
        parent = await session.get(Folder, parent_id)
    assert parent is not None
    assert parent.parent_id is None


@pytest.mark.asyncio
async def test_add_item_updates_folder_counts_when_reassigning(
    folder_db: DatabaseService,
) -> None:
    owner = uuid4()
    source_id = uuid4()
    target_id = uuid4()
    file_id = uuid4()
    await _add_user(folder_db, owner)
    await _add_folder(
        folder_db,
        folder_id=source_id,
        user_id=owner,
        name="source",
        file_count=1,
    )
    await _add_folder(folder_db, folder_id=target_id, user_id=owner, name="target")
    await _add_file(folder_db, file_id=file_id, user_id=owner, folder_id=source_id)

    await add_item_to_folder(
        AddItemRequest(file_id=file_id, folder_id=target_id),
        owner,
        folder_db,
    )
    await add_item_to_folder(
        AddItemRequest(file_id=file_id, folder_id=target_id),
        owner,
        folder_db,
    )

    async with folder_db.session() as session:
        source = await session.get(Folder, source_id)
        target = await session.get(Folder, target_id)
        row = await session.get(File, file_id)

    assert source is not None and source.file_count == 0
    assert target is not None and target.file_count == 1
    assert row is not None and row.folder_id == target_id
