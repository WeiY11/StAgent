"""SQLite dashed/hex UUID compatibility for file/document knowledge paths."""

from __future__ import annotations

import types
from datetime import datetime
from pathlib import Path  # noqa: TC003 - pytest fixture annotations are runtime-resolved
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text

from leagent.api.v1 import documents as documents_api
from leagent.api.v1.documents import search_documents
from leagent.api.v1.files import FileRenameRequest, delete_file, rename_file, search_files
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
            text("INSERT OR IGNORE INTO users (id, role, disabled) VALUES (:id, 'user', 0)"),
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


class _LimitAwareChunkRepo(_FakeChunkRepo):
    async def search(self, *_args: object, **kwargs: object) -> list[ChunkHit]:
        return self._hits[: int(kwargs["limit"])]


class _WrappedDb:
    def __init__(self, db: DatabaseService, hits: list[ChunkHit]) -> None:
        self._db = db
        self.repositories = types.SimpleNamespace(
            document_chunks=_FakeChunkRepo(hits),
            files=db.repositories.files,
        )

    def session(self):
        return self._db.session()


class _FailingChunkRepo:
    async def replace_for_file(self, *_args: object, **_kwargs: object) -> int:
        raise RuntimeError("chunk index unavailable")


class _FailingIndexDb:
    def __init__(self, db: DatabaseService) -> None:
        self._db = db
        self.repositories = types.SimpleNamespace(document_chunks=_FailingChunkRepo())

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
async def test_document_search_deduplicates_chunk_hits_and_returns_file_metadata(
    file_doc_db: DatabaseService,
    tmp_path: Path,
) -> None:
    """One document stays one result even when several chunks match."""
    user_id = uuid4()
    file_id = uuid4()
    storage_path = tmp_path / "catalog-paper.pdf"
    await _insert_dashed_file(
        file_doc_db,
        file_id=file_id,
        user_id=user_id,
        name="catalog-paper.pdf",
        storage_path=storage_path,
    )
    async with file_doc_db.session() as session:
        await session.execute(
            text("UPDATE files SET mime_type=:mime, summary=:summary, size=:size WHERE id=:id"),
            {
                "mime": "application/pdf",
                "summary": "Catalog summary",
                "size": 321,
                "id": str(file_id),
            },
        )

    first = _hit(file_id, file_name="catalog-paper.pdf")
    second = _hit(file_id, file_name="catalog-paper.pdf")
    second = ChunkHit(
        chunk_id=second.chunk_id,
        file_id=second.file_id,
        file_name=second.file_name,
        seq=1,
        start_offset=21,
        end_offset=40,
        text=second.text,
        snippet="second matching chunk",
        score=0.5,
    )
    db = _WrappedDb(file_doc_db, [first, second])

    response = await search_documents(
        user_id,
        db,  # type: ignore[arg-type]
        query="alpha",
        file_types=None,
        folder_id=None,
        limit=10,
    )

    assert response.total == 1
    assert len(response.results) == 1
    result = response.results[0]
    assert result.id == file_id
    assert result.name == "catalog-paper.pdf"
    assert result.mime_type == "application/pdf"
    assert result.size == 321
    assert result.status.value == "processed"
    assert result.is_indexed is True
    assert result.summary == "Catalog summary"
    assert result.created_at is not None


@pytest.mark.asyncio
async def test_document_search_limit_counts_documents_not_repeated_chunks(
    file_doc_db: DatabaseService,
    tmp_path: Path,
) -> None:
    """A long paper cannot crowd every other matching document out of the page."""
    user_id = uuid4()
    first_id = uuid4()
    second_id = uuid4()
    await _insert_dashed_file(
        file_doc_db,
        file_id=first_id,
        user_id=user_id,
        name="long-paper.pdf",
        storage_path=tmp_path / "long-paper.pdf",
    )
    await _insert_dashed_file(
        file_doc_db,
        file_id=second_id,
        user_id=user_id,
        name="short-paper.pdf",
        storage_path=tmp_path / "short-paper.pdf",
    )
    hits = [_hit(first_id, file_name="long-paper.pdf") for _ in range(8)]
    hits.append(_hit(second_id, file_name="short-paper.pdf"))
    wrapped = _WrappedDb(file_doc_db, hits)
    wrapped.repositories.document_chunks = _LimitAwareChunkRepo(hits)

    response = await search_documents(
        user_id,
        wrapped,  # type: ignore[arg-type]
        query="alpha",
        file_types=None,
        folder_id=None,
        limit=2,
    )

    assert response.total == 2
    assert [item.id for item in response.results] == [first_id, second_id]


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
async def test_delete_file_decrements_folder_count_for_dashed_sqlite_row(
    file_doc_db: DatabaseService,
    tmp_path: Path,
) -> None:
    """Deleting through the file API keeps the knowledge tree count truthful."""
    user_id = uuid4()
    file_id = uuid4()
    folder_id = uuid4()
    await _insert_dashed_file(
        file_doc_db,
        file_id=file_id,
        user_id=user_id,
        name="folder-paper.pdf",
        storage_path=tmp_path / "folder-paper.pdf",
    )
    async with file_doc_db.session() as session:
        now = datetime.utcnow().isoformat(sep=" ")
        await session.execute(
            text(
                "INSERT INTO folders "
                "(is_deleted, created_at, updated_at, id, name, user_id, "
                "position, file_count, flow_count, is_project) "
                "VALUES (0, :now, :now, :id, 'papers', :uid, 0, 1, 0, 0)"
            ),
            {"now": now, "id": str(folder_id), "uid": str(user_id)},
        )
        await session.execute(
            text("UPDATE files SET folder_id=:folder_id WHERE id=:file_id"),
            {"folder_id": str(folder_id), "file_id": str(file_id)},
        )

    await delete_file(file_id, user_id, file_doc_db)

    async with file_doc_db.session() as session:
        folder_count = (
            await session.execute(
                text(
                    "SELECT file_count FROM folders "
                    "WHERE lower(replace(CAST(id AS TEXT), '-', ''))=:id_hex"
                ),
                {"id_hex": folder_id.hex.lower()},
            )
        ).scalar_one()

    assert folder_count == 0


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
                text("SELECT extracted_text, is_indexed, summary FROM files WHERE id = :id"),
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


@pytest.mark.asyncio
async def test_file_processing_index_failure_is_retryable_and_not_marked_indexed(
    file_doc_db: DatabaseService,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A failed chunk write must not leave a false green indexed state."""
    user_id = uuid4()
    file_id = uuid4()
    path = tmp_path / "retry-index.txt"
    await _insert_dashed_file(
        file_doc_db,
        file_id=file_id,
        user_id=user_id,
        name="retry-index.txt",
        storage_path=path,
        extracted_text="old searchable text",
        library_scope="knowledge",
    )
    wrapped_db = _FailingIndexDb(file_doc_db)

    class _ServiceManager:
        db = wrapped_db

    monkeypatch.setattr(
        "leagent.services.service_manager.get_service_manager",
        lambda: _ServiceManager(),
    )
    service = FileProcessingService()

    async def _fake_process_file(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {
            "extracted_text": "fresh searchable text",
            "metadata": {},
            "handler": "text_processor",
        }

    monkeypatch.setattr(service, "process_file", _fake_process_file)

    with pytest.raises(RuntimeError, match="chunk index unavailable"):
        await service.process_and_update_db(
            str(file_id),
            str(path),
            mime_type="text/plain",
            original_name="retry-index.txt",
        )

    async with file_doc_db.session() as session:
        row = (
            await session.execute(
                text("SELECT status, is_indexed FROM files WHERE id=:id"),
                {"id": str(file_id)},
            )
        ).one()

    assert row._mapping["status"] == "failed"
    assert bool(row._mapping["is_indexed"]) is False


@pytest.mark.asyncio
async def test_file_processing_unexpected_extraction_failure_marks_document_failed(
    file_doc_db: DatabaseService,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Unexpected extractor crashes must not strand the catalog in processing."""
    user_id = uuid4()
    file_id = uuid4()
    path = tmp_path / "extractor-crash.txt"
    await _insert_dashed_file(
        file_doc_db,
        file_id=file_id,
        user_id=user_id,
        name="extractor-crash.txt",
        storage_path=path,
        library_scope="knowledge",
    )

    class _ServiceManager:
        db = file_doc_db

    monkeypatch.setattr(
        "leagent.services.service_manager.get_service_manager",
        lambda: _ServiceManager(),
    )
    service = FileProcessingService()

    async def _crash(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise RuntimeError("extractor crashed")

    monkeypatch.setattr(service, "process_file", _crash)

    with pytest.raises(RuntimeError, match="extractor crashed"):
        await service.process_and_update_db(
            str(file_id),
            str(path),
            mime_type="text/plain",
            original_name="extractor-crash.txt",
        )

    async with file_doc_db.session() as session:
        row = (
            await session.execute(
                text("SELECT status, is_indexed FROM files WHERE id=:id"),
                {"id": str(file_id)},
            )
        ).one()

    assert row._mapping["status"] == "failed"
    assert bool(row._mapping["is_indexed"]) is False


@pytest.mark.asyncio
async def test_reindex_endpoint_resets_state_and_enqueues_owned_knowledge_file(
    file_doc_db: DatabaseService,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A failed knowledge document can be explicitly requeued by its owner."""
    user_id = uuid4()
    file_id = uuid4()
    path = tmp_path / "failed-paper.pdf"
    await _insert_dashed_file(
        file_doc_db,
        file_id=file_id,
        user_id=user_id,
        name="failed-paper.pdf",
        storage_path=path,
        library_scope="knowledge",
    )
    async with file_doc_db.session() as session:
        await session.execute(
            text("UPDATE files SET status='failed', is_indexed=1 WHERE id=:id"),
            {"id": str(file_id)},
        )

    captured: dict[str, object] = {}

    async def _enqueue(_db: object, **kwargs: object) -> str:
        captured.update(kwargs)
        return "task-1"

    monkeypatch.setattr("leagent.tasks.jobs.enqueue_file_processing", _enqueue)
    endpoint = getattr(documents_api, "reindex_document", None)
    assert endpoint is not None, "documents.reindex_document endpoint is missing"

    response = await endpoint(file_id, user_id, file_doc_db)

    assert response.id == file_id
    assert response.status.value == "processing"
    assert response.is_indexed is False
    assert response.task_id == "task-1"
    assert captured["file_id"] == str(file_id)
    assert captured["storage_path"] == str(path)


@pytest.mark.asyncio
async def test_reindex_enqueue_failure_does_not_leave_document_processing(
    file_doc_db: DatabaseService,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Queue outages must leave a retryable failed state, not a stuck spinner."""
    user_id = uuid4()
    file_id = uuid4()
    path = tmp_path / "queue-outage.pdf"
    await _insert_dashed_file(
        file_doc_db,
        file_id=file_id,
        user_id=user_id,
        name="queue-outage.pdf",
        storage_path=path,
        library_scope="knowledge",
    )

    async def _fail_enqueue(_db: object, **_kwargs: object) -> str:
        raise RuntimeError("task queue unavailable")

    monkeypatch.setattr(
        "leagent.tasks.jobs.enqueue_file_processing",
        _fail_enqueue,
    )

    with pytest.raises(RuntimeError, match="task queue unavailable"):
        await documents_api.reindex_document(file_id, user_id, file_doc_db)

    async with file_doc_db.session() as session:
        row = (
            await session.execute(
                text("SELECT status, is_indexed FROM files WHERE id=:id"),
                {"id": str(file_id)},
            )
        ).one()

    assert row._mapping["status"] == "failed"
    assert bool(row._mapping["is_indexed"]) is False
