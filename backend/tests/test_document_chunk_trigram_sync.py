"""Regression coverage for the incremental CJK trigram index."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from sqlalchemy import text

from StAgent.config.settings import Settings
from StAgent.db.models.file import File, FileStatus, FileType, LibraryScope
from StAgent.db.service import DatabaseService
from StAgent.library.chunking import Chunk
from StAgent.library.fts import TRIGRAM_TABLE
from StAgent.services.auth.service import LOCAL_USER_ID

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.asyncio
async def test_zero_config_sqlite_indexes_new_chunk_for_cjk_trigram_search(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A chunk inserted after bootstrap is present in the real trigram index."""

    db_path = tmp_path / "trigram-sync.db"
    monkeypatch.setenv("DB_DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    db = DatabaseService(Settings())
    await db.create_tables()
    try:
        file_id = uuid4()
        async with db.session() as session:
            session.add(
                File(
                    id=file_id,
                    user_id=LOCAL_USER_ID,
                    name="cjk.txt",
                    original_name="cjk.txt",
                    file_type=FileType.DOCUMENT,
                    size=18,
                    status=FileStatus.PROCESSED,
                    storage_path=str(tmp_path / "cjk.txt"),
                    library_scope=LibraryScope.KNOWLEDGE,
                    is_indexed=True,
                )
            )

        await db.repositories.document_chunks.replace_for_file(
            file_id,
            LOCAL_USER_ID,
            [Chunk.with_hash(0, 0, 9, "火星轨道导航系统")],
        )

        async with db.session() as session:
            exists = (
                await session.execute(
                    text("SELECT 1 FROM sqlite_master WHERE type='table' AND name=:name"),
                    {"name": TRIGRAM_TABLE},
                )
            ).first()
            assert exists is not None, "zero-config SQLite must create the CJK trigram index"
            hit = (
                await session.execute(
                    text(f"SELECT rowid FROM {TRIGRAM_TABLE} WHERE {TRIGRAM_TABLE} MATCH :query"),
                    {"query": '"轨道导航"'},
                )
            ).first()

        assert hit is not None
    finally:
        await db.dispose()


@pytest.mark.asyncio
async def test_cjk_trigram_index_removes_stale_rows_after_update_and_delete(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The supplemental index follows direct updates and repository deletes."""

    db_path = tmp_path / "trigram-mutations.db"
    monkeypatch.setenv("DB_DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    db = DatabaseService(Settings())
    await db.create_tables()
    try:
        file_id = uuid4()
        async with db.session() as session:
            session.add(
                File(
                    id=file_id,
                    user_id=LOCAL_USER_ID,
                    name="cjk-mutation.txt",
                    original_name="cjk-mutation.txt",
                    file_type=FileType.DOCUMENT,
                    size=18,
                    status=FileStatus.PROCESSED,
                    storage_path=str(tmp_path / "cjk-mutation.txt"),
                    library_scope=LibraryScope.KNOWLEDGE,
                    is_indexed=True,
                )
            )

        await db.repositories.document_chunks.replace_for_file(
            file_id,
            LOCAL_USER_ID,
            [Chunk.with_hash(0, 0, 9, "火星轨道导航系统")],
        )

        async with db.session() as session:
            await session.execute(
                text("UPDATE document_chunks SET text = :text"),
                {"text": "深空通信链路稳定"},
            )
            stale_hit = (
                await session.execute(
                    text(f"SELECT rowid FROM {TRIGRAM_TABLE} WHERE {TRIGRAM_TABLE} MATCH :query"),
                    {"query": '"轨道导航"'},
                )
            ).first()
            current_hit = (
                await session.execute(
                    text(f"SELECT rowid FROM {TRIGRAM_TABLE} WHERE {TRIGRAM_TABLE} MATCH :query"),
                    {"query": '"通信链路"'},
                )
            ).first()

        assert stale_hit is None
        assert current_hit is not None

        await db.repositories.document_chunks.delete_for_file(file_id)
        async with db.session() as session:
            deleted_hit = (
                await session.execute(
                    text(f"SELECT rowid FROM {TRIGRAM_TABLE} WHERE {TRIGRAM_TABLE} MATCH :query"),
                    {"query": '"通信链路"'},
                )
            ).first()

        assert deleted_hit is None
    finally:
        await db.dispose()


@pytest.mark.asyncio
async def test_cjk_question_retrieval_matches_separated_document_terms(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A CJK question need not occur verbatim to find its lexical evidence."""

    db_path = tmp_path / "cjk-question.db"
    monkeypatch.setenv("DB_DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    db = DatabaseService(Settings())
    await db.create_tables()
    try:
        document_text = "北京是中国的首都。"
        file_id = uuid4()
        async with db.session() as session:
            session.add(
                File(
                    id=file_id,
                    user_id=LOCAL_USER_ID,
                    name="capital.txt",
                    original_name="capital.txt",
                    file_type=FileType.DOCUMENT,
                    size=len(document_text.encode("utf-8")),
                    status=FileStatus.PROCESSED,
                    storage_path=str(tmp_path / "capital.txt"),
                    library_scope=LibraryScope.KNOWLEDGE,
                    is_indexed=True,
                )
            )
        await db.repositories.document_chunks.replace_for_file(
            file_id,
            LOCAL_USER_ID,
            [Chunk.with_hash(0, 0, len(document_text), document_text)],
        )

        hits = await db.repositories.document_chunks.search(
            LOCAL_USER_ID,
            "中国首都在哪里？",
            limit=5,
            library_scope=LibraryScope.KNOWLEDGE.value,
        )

        assert [hit.file_id for hit in hits] == [file_id]
    finally:
        await db.dispose()
