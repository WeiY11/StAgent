"""SQLite dashed/hex UUID compatibility for file/document knowledge paths."""

from __future__ import annotations

import types
from datetime import datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text

from leagent.api.v1.documents import search_documents
from leagent.api.v1.files import FileRenameRequest, rename_file, search_files
from leagent.config.settings import Settings
from leagent.db.repositories.document_chunks import ChunkHit
from leagent.db.service import DatabaseService
from leagent.services.file_processing.service import FileProcessingService
from leagent.tools.base import ToolContext
from leagent.tools.doc.knowledge_search import KnowledgeSearchTool


@pytest_asyncio.fixture()
async def file_doc_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    db_path = tmp_path / "file-doc.db"
    monkeypatch.setenv("DB_DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    db = DatabaseService(Settings())
    await db.create_tables()
    try:
        yield db
    finally:
        await db.dispose()


async def _insert_dashed_file(
    db: DatabaseService,
    *,
    file_id: UUID,
    user_id: UUID,
    name: str,
    storage_path: Path,
    extracted_text: str = "alpha beta knowledge text",
    library_scope: str = "knowledge",
) -> None:
    now = datetime.utcnow().isoformat(sep=" ")
    storage_path.write_text(extracted_text, encoding="utf-8")
    async with db.session() as session:
        await session.execute(
            text("INSERT INTO users (id, role, disabled) VALUES (:id, 'user', 0)"),
            {"id": str(user_id)},
        )
        await session.execute(
            text(
                "INSERT INTO files "
                "(is_deleted, created_at, updated_at, id, name, original_name, "
                "file_type, size, status, user_id, storage_path, extracted_text, "
                "has_ocr, is_indexed, library_scope, inbox_state, is_pinned) "
                "VALUES (0, :now, :now, :id, :name, :name, 'document', :size, "
                "'processed', :uid, :path, :text, 0, 1, :scope, 'new', 0)"
            ),
            {
                "now": now,
                "id": str(file_id),
                "name": name,
                "size": len(extracted_text.encode("utf-8")),
                "uid": str(user_id),
                "path": str(storage_path),
                "text": extracted_text,
                "scope": library_scope,
            },
        )


def _hit(file_id: UUID, *, file_name: str = "kb.txt") -> ChunkHit:
    return ChunkHit(
        chunk_id=uuid4(),
        file_id=file_id,
        file_name=file_name,
        seq=0,
        start_offset=0,
        end_offset=20,
        text="alpha beta knowledge",
        snippet="alpha beta knowledge",
        score=1.0,
    )


class _FakeChunkRepo:
    def __init__(self, hits: list[ChunkHit]) -> None:
        self._hits = hits

    async def search(self, *_args: object, **_kwargs: object) -> list[ChunkHit]:
        return self._hits


class _WrappedDb:
    def __init__(self, db: DatabaseService, hits: list[ChunkHit]) -> None:
        self._db = db
        self.repositories = types.SimpleNamespace(
            document_chunks=_FakeChunkRepo(hits),
            files=db.repositories.files,
        )

    def session(self):
        return self._db.session()


@pytest.mark.asyncio
async def test_file_and_document_search_recheck_dashed_sqlite_chunk_hits(
    file_doc_db: DatabaseService,
    tmp_path: Path,
) -> None:
    user_id = uuid4()
    file_id = uuid4()
    await _insert_dashed_file(
        file_doc_db,
        file_id=file_id,
        user_id=user_id,
        name="kb.txt",
        storage_path=tmp_path / "kb.txt",
    )
    db = _WrappedDb(file_doc_db, [_hit(file_id)])

    files = await search_files(
        user_id,
        db,  # type: ignore[arg-type]
        query="alpha",
        library_scope=None,
        limit=10,
    )
    docs = await search_documents(
        user_id,
        db,  # type: ignore[arg-type]
        query="alpha",
        file_types=None,
        folder_id=None,
        limit=10,
    )

    assert [r.file_id for r in files.results] == [file_id]
    assert [r.id for r in docs.results] == [file_id]


@pytest.mark.asyncio
async def test_rename_file_updates_dashed_sqlite_file_row(
    file_doc_db: DatabaseService,
    tmp_path: Path,
) -> None:
    user_id = uuid4()
    file_id = uuid4()
    await _insert_dashed_file(
        file_doc_db,
        file_id=file_id,
        user_id=user_id,
        name="old.txt",
        storage_path=tmp_path / "old.txt",
    )

    renamed = await rename_file(
        file_id,
        FileRenameRequest(name="new unsafe name.txt"),
        user_id,
        file_doc_db,
    )

    async with file_doc_db.session() as session:
        name = (
            await session.execute(
                text("SELECT name FROM files WHERE id = :id"),
                {"id": str(file_id)},
            )
        ).scalar_one()

    assert renamed.name == "new_unsafe_name.txt"
    assert name == "new_unsafe_name.txt"


@pytest.mark.asyncio
async def test_knowledge_search_reads_dashed_sqlite_file_rows(
    file_doc_db: DatabaseService,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    user_id = uuid4()
    file_id = uuid4()
    await _insert_dashed_file(
        file_doc_db,
        file_id=file_id,
        user_id=user_id,
        name="kb.txt",
        storage_path=tmp_path / "kb.txt",
    )
    db = _WrappedDb(file_doc_db, [_hit(file_id)])
    monkeypatch.setattr("leagent.db.get_database_service", lambda: db)

    tool = KnowledgeSearchTool()
    ctx = ToolContext(user_id=str(user_id), session_id=None)
    search = await tool.execute({"action": "search", "query": "alpha"}, ctx)
    read = await tool.execute({"action": "read", "file_id": str(file_id)}, ctx)

    assert [r["file_id"] for r in search["results"]] == [str(file_id)]
    assert read["file_id"] == str(file_id)
    assert "alpha beta" in read["text"]


@pytest.mark.asyncio
async def test_file_processing_updates_and_indexes_dashed_sqlite_file_row(
    file_doc_db: DatabaseService,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    user_id = uuid4()
    file_id = uuid4()
    path = tmp_path / "processing.txt"
    await _insert_dashed_file(
        file_doc_db,
        file_id=file_id,
        user_id=user_id,
        name="processing.txt",
        storage_path=path,
        extracted_text="old text",
        library_scope="knowledge",
    )

    class _ServiceManager:
        db = file_doc_db

    monkeypatch.setattr(
        "leagent.services.service_manager.get_service_manager",
        lambda: _ServiceManager(),
    )
    service = FileProcessingService()

    async def _fake_process_file(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {
            "extracted_text": "fresh alpha beta chunk text",
            "metadata": {},
            "handler": "text_processor",
        }

    monkeypatch.setattr(service, "process_file", _fake_process_file)

    result = await service.process_and_update_db(
        str(file_id),
        str(path),
        mime_type="text/plain",
        original_name="processing.txt",
    )

    async with file_doc_db.session() as session:
        file_row = (
            await session.execute(
                text(
                    "SELECT extracted_text, is_indexed, summary "
                    "FROM files WHERE id = :id"
                ),
                {"id": str(file_id)},
            )
        ).one()
        chunk_count = (
            await session.execute(
                text(
                    "SELECT COUNT(*) FROM document_chunks "
                    "WHERE lower(replace(CAST(file_id AS TEXT), '-', '')) = :id_hex"
                ),
                {"id_hex": file_id.hex.lower()},
            )
        ).scalar_one()

    assert result["extracted_text"] == "fresh alpha beta chunk text"
    assert file_row._mapping["extracted_text"] == "fresh alpha beta chunk text"
    assert bool(file_row._mapping["is_indexed"]) is True
    assert file_row._mapping["summary"]
    assert chunk_count >= 1
