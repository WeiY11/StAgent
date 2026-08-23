from __future__ import annotations

import json
from contextlib import asynccontextmanager
from uuid import uuid4

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from StAgent.api.v1.tasks import get_task_files
from StAgent.db.models.file import File, FileType
from StAgent.db.models.task import Task, TaskStatus, TaskType


class _DB:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @asynccontextmanager
    async def session(self):
        yield self._session


def _file(*, name: str, user_id, deleted: bool = False) -> File:
    return File(
        name=name,
        original_name=name,
        file_type=FileType.DOCUMENT,
        size=1,
        storage_path=f"/tmp/{name}",
        user_id=user_id,
        is_deleted=deleted,
    )


@pytest.mark.asyncio
async def test_get_task_files_does_not_return_files_owned_by_other_users(
    db_session: AsyncSession,
) -> None:
    owner = uuid4()
    other = uuid4()
    own_file = _file(name="own.txt", user_id=owner)
    other_file = _file(name="other.txt", user_id=other)
    deleted_file = _file(name="deleted.txt", user_id=owner, deleted=True)
    task = Task(
        name="task",
        task_type=TaskType.AGENT,
        status=TaskStatus.COMPLETED,
        user_id=owner,
        output_data=json.dumps(
            {"files": [str(own_file.id), str(other_file.id), str(deleted_file.id)]}
        ),
    )
    db_session.add_all([own_file, other_file, deleted_file, task])
    await db_session.flush()
    db = _DB(db_session)

    files = await get_task_files(task.id, owner, db)  # type: ignore[arg-type]

    assert [f.id for f in files] == [own_file.id]
