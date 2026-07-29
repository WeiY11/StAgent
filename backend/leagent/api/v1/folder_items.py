"""Folder-item association management API endpoints."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import update as sa_update
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from leagent.db import DatabaseService, get_database_service
from leagent.db.models.file import File
from leagent.db.models.folder import Folder
from leagent.db.sqlite_compat import load_entity_by_id, same_user_id, uuid_column_equals
from leagent.services.auth import CurrentUserId

router = APIRouter()


def _assert_folder_owner(folder: Folder, user_id: UUID) -> None:
    if not same_user_id(folder.user_id, user_id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied to folder")


def _assert_file_owner(file: File, user_id: UUID) -> None:
    if not same_user_id(file.user_id, user_id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied to file")


async def _decrement_owned_folder_count(
    session: AsyncSession,
    folder_id: UUID | None,
    user_id: UUID,
) -> None:
    if folder_id is None:
        return
    statement = (
        sa_update(Folder)
        .where(uuid_column_equals(session, Folder.id, folder_id))
        .where(uuid_column_equals(session, Folder.user_id, user_id))
        .where(Folder.is_deleted == False)  # noqa: E712
        .where(Folder.file_count > 0)
        .values(file_count=Folder.file_count - 1, updated_at=datetime.utcnow())
        .execution_options(synchronize_session=False)
    )
    await session.execute(statement)


class FolderItemResponse(BaseModel):
    file_id: UUID
    folder_id: UUID
    file_name: str
    file_type: str
    size: int
    mime_type: str | None = None


class AddItemRequest(BaseModel):
    file_id: UUID
    folder_id: UUID


class MoveItemRequest(BaseModel):
    to_folder_id: UUID


class ReorderRequest(BaseModel):
    file_ids: list[UUID]


@router.get("", response_model=list[FolderItemResponse])
async def list_folder_items(
    folder_id: UUID,
    user_id: CurrentUserId,
    db: Annotated[DatabaseService, Depends(get_database_service)],
) -> list[FolderItemResponse]:
    """List all files in a folder."""
    async with db.session() as session:
        folder = await load_entity_by_id(session, Folder, folder_id, parent_table="folders")
        if not folder or folder.is_deleted:
            raise HTTPException(status_code=404, detail="Folder not found")
        _assert_folder_owner(folder, user_id)

        stmt = (
            select(File)
            .where(uuid_column_equals(session, File.folder_id, folder_id))
            .where(uuid_column_equals(session, File.user_id, user_id))
            .where(File.is_deleted == False)  # noqa: E712
        )
        result = await session.exec(stmt)
        files = result.all()

        return [
            FolderItemResponse(
                file_id=f.id,
                folder_id=folder_id,
                file_name=f.original_name,
                file_type=f.file_type.value,
                size=f.size,
                mime_type=f.mime_type,
            )
            for f in files
        ]


@router.post("", response_model=FolderItemResponse, status_code=status.HTTP_201_CREATED)
async def add_item_to_folder(
    data: AddItemRequest,
    user_id: CurrentUserId,
    db: Annotated[DatabaseService, Depends(get_database_service)],
) -> FolderItemResponse:
    """Add a file to a folder."""
    async with db.session() as session:
        folder = await load_entity_by_id(session, Folder, data.folder_id, parent_table="folders")
        if not folder or folder.is_deleted:
            raise HTTPException(status_code=404, detail="Folder not found")
        _assert_folder_owner(folder, user_id)

        f = await load_entity_by_id(session, File, data.file_id, parent_table="files")
        if not f or f.is_deleted:
            raise HTTPException(status_code=404, detail="File not found")
        _assert_file_owner(f, user_id)

        old_folder_id = f.folder_id
        if old_folder_id is not None and same_user_id(old_folder_id, data.folder_id):
            return FolderItemResponse(
                file_id=f.id,
                folder_id=data.folder_id,
                file_name=f.original_name,
                file_type=f.file_type.value,
                size=f.size,
                mime_type=f.mime_type,
            )

        f.folder_id = data.folder_id
        f.updated_at = datetime.utcnow()
        session.add(f)

        await _decrement_owned_folder_count(session, old_folder_id, user_id)

        folder.file_count = int(folder.file_count or 0) + 1
        session.add(folder)

        return FolderItemResponse(
            file_id=f.id,
            folder_id=data.folder_id,
            file_name=f.original_name,
            file_type=f.file_type.value,
            size=f.size,
            mime_type=f.mime_type,
        )


@router.delete("/{folder_id}/{file_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_item_from_folder(
    folder_id: UUID,
    file_id: UUID,
    user_id: CurrentUserId,
    db: Annotated[DatabaseService, Depends(get_database_service)],
) -> None:
    """Remove a file from a folder (unlink, not delete)."""
    async with db.session() as session:
        f = await load_entity_by_id(session, File, file_id, parent_table="files")
        if not f or f.is_deleted:
            raise HTTPException(status_code=404, detail="File not found")
        _assert_file_owner(f, user_id)

        if f.folder_id != folder_id:
            raise HTTPException(status_code=400, detail="File is not in this folder")

        f.folder_id = None
        f.updated_at = datetime.utcnow()
        session.add(f)

        folder = await load_entity_by_id(session, Folder, folder_id, parent_table="folders")
        if folder and not folder.is_deleted:
            _assert_folder_owner(folder, user_id)
        if folder and folder.file_count > 0:
            folder.file_count = folder.file_count - 1
            session.add(folder)


@router.put("/{file_id}/move")
async def move_item(
    file_id: UUID,
    data: MoveItemRequest,
    user_id: CurrentUserId,
    db: Annotated[DatabaseService, Depends(get_database_service)],
) -> dict:
    """Move a file to a different folder."""
    async with db.session() as session:
        f = await load_entity_by_id(session, File, file_id, parent_table="files")
        if not f or f.is_deleted:
            raise HTTPException(status_code=404, detail="File not found")
        _assert_file_owner(f, user_id)

        target = await load_entity_by_id(session, Folder, data.to_folder_id, parent_table="folders")
        if not target or target.is_deleted:
            raise HTTPException(status_code=404, detail="Target folder not found")
        _assert_folder_owner(target, user_id)

        old_folder_id = f.folder_id
        f.folder_id = data.to_folder_id
        f.updated_at = datetime.utcnow()
        session.add(f)

        if old_folder_id is not None and same_user_id(old_folder_id, data.to_folder_id):
            return {
                "file_id": str(file_id),
                "from_folder_id": str(old_folder_id),
                "to_folder_id": str(data.to_folder_id),
            }

        await _decrement_owned_folder_count(session, old_folder_id, user_id)

        target.file_count = int(target.file_count or 0) + 1
        session.add(target)

        return {
            "file_id": str(file_id),
            "from_folder_id": str(old_folder_id) if old_folder_id else None,
            "to_folder_id": str(data.to_folder_id),
        }
