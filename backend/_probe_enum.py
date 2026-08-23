"""Probe: what raw string does SQLite currently store for enum columns?"""
import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import SQLModel

from StAgent.db import models as _models  # noqa: F401  import all models
from StAgent.db.models.file import File, FileType, FileStatus, LibraryScope, InboxState


async def main() -> None:
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    # Insert a row via raw SQL bypassing FK by using a fresh minimal insert
    from uuid import uuid4
    from sqlalchemy.ext.asyncio import AsyncSession

    async with AsyncSession(eng) as sess:
        f = File(
            id=uuid4(),
            name="x",
            original_name="x",
            file_type=FileType.DOCUMENT,
            status=FileStatus.PROCESSED,
            storage_path="/tmp/x",
            library_scope=LibraryScope.KNOWLEDGE,
            inbox_state=InboxState.TRIAGED,
        )
        sess.add(f)
        await sess.commit()

    async with eng.begin() as conn:
        rows = (
            await conn.execute(
                text("SELECT file_type, status, library_scope, inbox_state FROM files")
            )
        ).fetchall()
        print("SQLITE_RAW", rows)
    await eng.dispose()


asyncio.run(main())
