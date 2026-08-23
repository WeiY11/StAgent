"""Tests for the unified library layer (chunking, FTS, GC, provenance)."""

from __future__ import annotations

import os
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from StAgent.library.chunking import chunk_text
from StAgent.library.fts import FTS_DDL, escape_fts_query
from StAgent.library.gc import run_file_gc


def test_chunk_text_preserves_offsets() -> None:
    text = "Paragraph one.\n\nParagraph two has more words."
    chunks = chunk_text(text, max_chars=40, overlap=5)
    assert len(chunks) >= 1
    for ch in chunks:
        assert text[ch.start_offset : ch.end_offset] == ch.text


def test_escape_fts_query_quotes_tokens() -> None:
    q = escape_fts_query('hello "world"')
    assert '"hello"*' in q
    assert q  # non-empty


def test_fts_ddl_is_non_empty() -> None:
    assert len(FTS_DDL) >= 4


@pytest.mark.asyncio
async def test_run_file_gc_removes_orphan_blob(monkeypatch: pytest.MonkeyPatch) -> None:
    from StAgent.config.settings import Settings
    from StAgent.db.models.file import File, FileStatus, FileType, LibraryScope
    from StAgent.db.service import DatabaseService

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "gc.db"
        monkeypatch.setenv("DB_DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
        settings = Settings()
        db = DatabaseService(settings)
        await db.create_tables()

        from StAgent.services.auth.service import LOCAL_USER_ID

        blob = Path(tmp) / "orphan.bin"
        blob.write_bytes(b"deadbeef")
        uid = LOCAL_USER_ID
        fid = uuid4()
        now = datetime.now(UTC).replace(tzinfo=None)
        old = now - timedelta(days=10)

        async with db.session() as session:
            session.add(
                File(
                    id=fid,
                    user_id=uid,
                    name="orphan.bin",
                    original_name="orphan.bin",
                    file_type=FileType.OTHER,
                    size=8,
                    status=FileStatus.PROCESSED,
                    storage_path=str(blob),
                    checksum="abc",
                    library_scope=LibraryScope.WORKSPACE,
                    is_deleted=True,
                    deleted_at=old,
                )
            )

        summary = await run_file_gc(db, grace_hours=24)
        assert summary["removed_blobs"] == 1
        assert not blob.exists()

        await db.dispose()
