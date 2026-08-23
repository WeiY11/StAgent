"""SQLite dashed/hex UUID compatibility for file/document knowledge paths."""

from __future__ import annotations

import types
from datetime import datetime
from pathlib import Path  # noqa: TC003 - pytest fixture annotations are runtime-resolved
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import text

from StAgent.api.v1 import documents as documents_api
from StAgent.api.v1 import knowledge as knowledge_api
from StAgent.api.v1.documents import search_documents
from StAgent.api.v1.files import (
    FileRenameRequest,
    _resolve_file_for_serve,
    delete_file,
    rename_file,
    search_files,
)
from StAgent.config.settings import Settings
from StAgent.db.models.knowledge_index import KnowledgeIndexStatus
from StAgent.db.repositories.document_chunks import ChunkHit
from StAgent.db.service import DatabaseService
from StAgent.knowledge.acl import ensure_personal_knowledge_space
from StAgent.library.chunking import chunk_auto
from StAgent.services.file_processing.service import FileProcessingService
from StAgent.tools.base import ToolContext
from StAgent.tools.doc.knowledge_search import KnowledgeSearchTool


@pytest_asyncio.fixture()
async def file_doc_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    db_path = tmp_path / "file-doc.db"
    monkeypatch.setenv("DB_DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    # App-lifespan tests (any `client` fixture) wire a module-level retrieval
    # pipeline bound to the shared app DB, and the still-running app can
    # re-wire it between tests. Force the tool's per-db fallback path so this
    # suite always queries its own database.
    import StAgent.library.retrieval as _retrieval

    monkeypatch.setattr(_retrieval, "_pipeline", None)
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
                text("SELECT file_count FROM folders WHERE lower(replace(CAST(id AS TEXT), '-', ''))=:id_hex"),
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
    monkeypatch.setattr("StAgent.db.get_database_service", lambda: db)

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
        "StAgent.services.service_manager.get_service_manager",
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
async def test_file_processing_reindexes_changed_content_after_existing_index(
    file_doc_db: DatabaseService,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A changed upload must compare against the hash from before the upsert."""
    user_id = uuid4()
    file_id = uuid4()
    path = tmp_path / "changed-content.txt"
    await _insert_dashed_file(
        file_doc_db,
        file_id=file_id,
        user_id=user_id,
        name="changed-content.txt",
        storage_path=path,
        extracted_text="initial source text",
        library_scope="knowledge",
    )

    class _ServiceManager:
        db = file_doc_db

    monkeypatch.setattr(
        "StAgent.services.service_manager.get_service_manager",
        lambda: _ServiceManager(),
    )
    service = FileProcessingService()
    extractions = iter(("initial source text", "updated source text"))

    async def _fake_process_file(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {
            "extracted_text": next(extractions),
            "metadata": {},
            "handler": "text_processor",
        }

    monkeypatch.setattr(service, "process_file", _fake_process_file)

    first = await service.process_and_update_db(
        str(file_id),
        str(path),
        mime_type="text/plain",
        original_name="changed-content.txt",
    )
    second = await service.process_and_update_db(
        str(file_id),
        str(path),
        mime_type="text/plain",
        original_name="changed-content.txt",
    )

    async with file_doc_db.session() as session:
        chunk_texts = (
            (
                await session.execute(
                    text(
                        "SELECT text FROM document_chunks "
                        "WHERE lower(replace(CAST(file_id AS TEXT), '-', '')) = :id_hex "
                        "ORDER BY seq"
                    ),
                    {"id_hex": file_id.hex.lower()},
                )
            )
            .scalars()
            .all()
        )

    assert "_index_skipped" not in first
    assert "_index_skipped" not in second
    assert chunk_texts
    assert "updated source text" in chunk_texts[0]


@pytest.mark.asyncio
async def test_file_processing_reindexes_unchanged_content_after_index_version_upgrade(
    file_doc_db: DatabaseService,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A new RAG index version must rebuild an otherwise unchanged document."""
    user_id = uuid4()
    file_id = uuid4()
    path = tmp_path / "versioned-content.txt"
    await _insert_dashed_file(
        file_doc_db,
        file_id=file_id,
        user_id=user_id,
        name="versioned-content.txt",
        storage_path=path,
        extracted_text="initial source text",
        library_scope="knowledge",
    )

    class _ServiceManager:
        db = file_doc_db

    knowledge_settings = types.SimpleNamespace(
        index_version="rag-v1",
        opensearch_url="",
    )
    monkeypatch.setattr(
        "StAgent.services.service_manager.get_service_manager",
        lambda: _ServiceManager(),
    )
    monkeypatch.setattr(
        "StAgent.config.settings.get_settings",
        lambda: types.SimpleNamespace(knowledge=knowledge_settings),
    )
    service = FileProcessingService()

    async def _fake_process_file(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {
            "extracted_text": "stable source text",
            "metadata": {},
            "handler": "text_processor",
        }

    monkeypatch.setattr(service, "process_file", _fake_process_file)

    first = await service.process_and_update_db(
        str(file_id),
        str(path),
        mime_type="text/plain",
        original_name="versioned-content.txt",
    )
    unchanged = await service.process_and_update_db(
        str(file_id),
        str(path),
        mime_type="text/plain",
        original_name="versioned-content.txt",
    )
    knowledge_settings.index_version = "rag-v2"
    reindexed = await service.process_and_update_db(
        str(file_id),
        str(path),
        mime_type="text/plain",
        original_name="versioned-content.txt",
    )

    async with file_doc_db.session() as session:
        state = (
            await session.execute(
                text(
                    "SELECT index_version, status FROM knowledge_index_states "
                    "WHERE lower(replace(CAST(file_id AS TEXT), '-', '')) = :id_hex"
                ),
                {"id_hex": file_id.hex.lower()},
            )
        ).one()

    assert "_index_skipped" not in first
    assert unchanged["_index_skipped"] is True
    assert "_index_skipped" not in reindexed
    assert state._mapping["index_version"] == "rag-v2"
    assert state._mapping["status"] == "ready"


@pytest.mark.asyncio
async def test_bulk_reindex_queues_only_stale_writable_documents(
    file_doc_db: DatabaseService,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A version upgrade must queue only documents the caller can rebuild."""
    user_id = uuid4()
    other_user_id = uuid4()
    stale_file_id = uuid4()
    current_file_id = uuid4()
    other_file_id = uuid4()
    await _insert_dashed_file(
        file_doc_db,
        file_id=stale_file_id,
        user_id=user_id,
        name="stale.txt",
        storage_path=tmp_path / "stale.txt",
    )
    await _insert_dashed_file(
        file_doc_db,
        file_id=current_file_id,
        user_id=user_id,
        name="current.txt",
        storage_path=tmp_path / "current.txt",
    )
    await _insert_dashed_file(
        file_doc_db,
        file_id=other_file_id,
        user_id=other_user_id,
        name="other.txt",
        storage_path=tmp_path / "other.txt",
    )
    await file_doc_db.repositories.knowledge_index.upsert_state(
        stale_file_id,
        user_id,
        status=KnowledgeIndexStatus.READY,
        index_version="rag-v1",
        content_hash="old",
    )
    await file_doc_db.repositories.knowledge_index.upsert_state(
        current_file_id,
        user_id,
        status=KnowledgeIndexStatus.READY,
        index_version="rag-v2",
        content_hash="current",
    )
    await file_doc_db.repositories.knowledge_index.upsert_state(
        other_file_id,
        other_user_id,
        status=KnowledgeIndexStatus.READY,
        index_version="rag-v1",
        content_hash="other",
    )

    queued: list[dict[str, object]] = []

    async def _enqueue(*_args: object, **kwargs: object) -> str:
        queued.append(kwargs)
        return "task-1"

    monkeypatch.setattr(
        "StAgent.config.settings.get_settings",
        lambda: types.SimpleNamespace(knowledge=types.SimpleNamespace(index_version="rag-v2")),
    )
    monkeypatch.setattr("StAgent.tasks.jobs.enqueue_file_processing", _enqueue)

    endpoint = getattr(knowledge_api, "reindex_stale_knowledge_documents", None)
    assert endpoint is not None, "knowledge bulk reindex endpoint is missing"
    response = await endpoint(
        knowledge_api.KnowledgeBulkReindexRequest(limit=10),
        user_id,
        file_doc_db,
    )

    async with file_doc_db.session() as session:
        statuses = dict(
            (
                await session.execute(
                    text("SELECT id, status FROM files WHERE id IN (:stale, :current, :other)"),
                    {
                        "stale": str(stale_file_id),
                        "current": str(current_file_id),
                        "other": str(other_file_id),
                    },
                )
            ).all()
        )

    assert response.index_version == "rag-v2"
    assert response.matched == 1
    assert response.accepted == 1
    assert response.unavailable == 0
    assert [item["file_id"] for item in queued] == [str(stale_file_id)]
    assert statuses[str(stale_file_id)] == "processing"
    assert statuses[str(current_file_id)] == "processed"
    assert statuses[str(other_file_id)] == "processed"


@pytest.mark.asyncio
async def test_bulk_reindex_marks_unavailable_source_as_failed(
    file_doc_db: DatabaseService,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A stale document with no recoverable source cannot remain falsely ready."""
    user_id = uuid4()
    file_id = uuid4()
    await _insert_dashed_file(
        file_doc_db,
        file_id=file_id,
        user_id=user_id,
        name="missing-source.txt",
        storage_path=tmp_path / "existing.txt",
    )
    await file_doc_db.repositories.knowledge_index.upsert_state(
        file_id,
        user_id,
        status=KnowledgeIndexStatus.READY,
        index_version="rag-v1",
        content_hash="old",
    )
    async with file_doc_db.session() as session:
        await session.execute(
            text("UPDATE files SET storage_path = :path WHERE id = :id"),
            {"path": str(tmp_path / "missing.txt"), "id": str(file_id)},
        )

    queued: list[dict[str, object]] = []

    async def _enqueue(*_args: object, **kwargs: object) -> str:
        queued.append(kwargs)
        return "task-1"

    monkeypatch.setattr(
        "StAgent.config.settings.get_settings",
        lambda: types.SimpleNamespace(knowledge=types.SimpleNamespace(index_version="rag-v2")),
    )
    monkeypatch.setattr("StAgent.tasks.jobs.enqueue_file_processing", _enqueue)

    response = await knowledge_api.reindex_stale_knowledge_documents(
        knowledge_api.KnowledgeBulkReindexRequest(limit=10),
        user_id,
        file_doc_db,
    )

    async with file_doc_db.session() as session:
        file_row = (
            await session.execute(
                text("SELECT status, is_indexed FROM files WHERE id = :id"),
                {"id": str(file_id)},
            )
        ).one()
        state_row = (
            await session.execute(
                text(
                    "SELECT index_version, status, last_error FROM knowledge_index_states "
                    "WHERE lower(replace(CAST(file_id AS TEXT), '-', '')) = :id_hex"
                ),
                {"id_hex": file_id.hex.lower()},
            )
        ).one()

    assert response.matched == 1
    assert response.accepted == 0
    assert response.unavailable == 1
    assert queued == []
    assert file_row._mapping["status"] == "failed"
    assert bool(file_row._mapping["is_indexed"]) is False
    assert state_row._mapping["index_version"] == "rag-v2"
    assert state_row._mapping["status"] == "failed"
    assert state_row._mapping["last_error"] == "reindex_source_unavailable"


@pytest.mark.asyncio
async def test_bulk_reindex_repairs_state_when_task_enqueue_fails(
    file_doc_db: DatabaseService,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Queue errors must not strand a stale document in processing."""
    user_id = uuid4()
    file_id = uuid4()
    await _insert_dashed_file(
        file_doc_db,
        file_id=file_id,
        user_id=user_id,
        name="queue-failure.txt",
        storage_path=tmp_path / "queue-failure.txt",
    )
    await file_doc_db.repositories.knowledge_index.upsert_state(
        file_id,
        user_id,
        status=KnowledgeIndexStatus.READY,
        index_version="rag-v1",
        content_hash="old",
    )

    async def _enqueue(*_args: object, **_kwargs: object) -> str:
        raise RuntimeError("task queue unavailable")

    monkeypatch.setattr(
        "StAgent.config.settings.get_settings",
        lambda: types.SimpleNamespace(knowledge=types.SimpleNamespace(index_version="rag-v2")),
    )
    monkeypatch.setattr("StAgent.tasks.jobs.enqueue_file_processing", _enqueue)

    response = await knowledge_api.reindex_stale_knowledge_documents(
        knowledge_api.KnowledgeBulkReindexRequest(limit=10),
        user_id,
        file_doc_db,
    )

    async with file_doc_db.session() as session:
        file_row = (
            await session.execute(
                text("SELECT status, is_indexed FROM files WHERE id = :id"),
                {"id": str(file_id)},
            )
        ).one()
        state_row = (
            await session.execute(
                text(
                    "SELECT index_version, status, last_error FROM knowledge_index_states "
                    "WHERE lower(replace(CAST(file_id AS TEXT), '-', '')) = :id_hex"
                ),
                {"id_hex": file_id.hex.lower()},
            )
        ).one()

    assert response.matched == 1
    assert response.accepted == 0
    assert response.unavailable == 0
    assert file_row._mapping["status"] == "failed"
    assert bool(file_row._mapping["is_indexed"]) is False
    assert state_row._mapping["index_version"] == "rag-v2"
    assert state_row._mapping["status"] == "failed"
    assert state_row._mapping["last_error"] == "reindex_enqueue_failed"


@pytest.mark.asyncio
async def test_knowledge_index_queue_is_idempotent_and_claimable(
    file_doc_db: DatabaseService,
    tmp_path: Path,
) -> None:
    """Index state/job rows survive legacy UUID text and duplicate enqueue calls."""
    user_id = uuid4()
    file_id = uuid4()
    await _insert_dashed_file(
        file_doc_db,
        file_id=file_id,
        user_id=user_id,
        name="queued.txt",
        storage_path=tmp_path / "queued.txt",
        library_scope="knowledge",
    )

    repo = file_doc_db.repositories.knowledge_index
    state = await repo.upsert_state(
        file_id,
        user_id,
        status=KnowledgeIndexStatus.EXTRACTING,
        content_hash="hash-v1",
    )
    first_job = await repo.enqueue(
        file_id,
        user_id,
        content_hash="hash-v1",
    )
    duplicate_job = await repo.enqueue(
        file_id,
        user_id,
        content_hash="hash-v1",
    )
    claimed = await repo.claim_batch("worker-a", limit=5)

    assert state.file_id == file_id
    assert first_job == duplicate_job
    assert len(claimed) == 1
    assert claimed[0].id == first_job
    assert claimed[0].attempt_count == 1

    await repo.mark_failed(first_job, "temporary embedding outage", retry_after_seconds=60)

    # Make the retry deadline due without sleeping; failed jobs must remain
    # claimable and retain their attempt history.
    async with file_doc_db.session() as session:
        await session.execute(
            text(
                "UPDATE knowledge_index_jobs SET available_at=:available_at "
                "WHERE lower(replace(CAST(id AS TEXT), '-', ''))=:id_hex"
            ),
            {
                "available_at": datetime.utcnow(),
                "id_hex": first_job.hex.lower(),
            },
        )

    retried = await repo.claim_batch("worker-b", limit=5)
    assert len(retried) == 1
    assert retried[0].id == first_job
    assert retried[0].attempt_count == 2

    await repo.mark_succeeded(first_job, chunk_count=3, embedded_count=3)

    async with file_doc_db.session() as session:
        row = (
            await session.execute(
                text(
                    "SELECT status, chunk_count, embedded_count "
                    "FROM knowledge_index_states WHERE lower(replace(CAST(file_id AS TEXT), '-', ''))=:id_hex"
                ),
                {"id_hex": file_id.hex.lower()},
            )
        ).one()
        job_row = (
            await session.execute(
                text("SELECT status FROM knowledge_index_jobs WHERE id=:id"),
                {"id": str(first_job)},
            )
        ).one()

    assert row._mapping["status"] == KnowledgeIndexStatus.READY.value
    assert row._mapping["chunk_count"] == 3
    assert row._mapping["embedded_count"] == 3
    assert job_row._mapping["status"] == "succeeded"


@pytest.mark.asyncio
async def test_stale_index_job_cannot_overwrite_newer_projection(
    file_doc_db: DatabaseService,
    tmp_path: Path,
) -> None:
    user_id = uuid4()
    file_id = uuid4()
    await _insert_dashed_file(
        file_doc_db,
        file_id=file_id,
        user_id=user_id,
        name="versioned.txt",
        storage_path=tmp_path / "versioned.txt",
        library_scope="knowledge",
    )

    repo = file_doc_db.repositories.knowledge_index
    await repo.upsert_state(
        file_id,
        user_id,
        status=KnowledgeIndexStatus.EXTRACTING,
        content_hash="hash-v1",
    )
    old_job = await repo.enqueue(file_id, user_id, content_hash="hash-v1")
    await repo.claim_batch("worker-a", limit=1)

    await repo.upsert_state(
        file_id,
        user_id,
        status=KnowledgeIndexStatus.EXTRACTING,
        content_hash="hash-v2",
    )
    await repo.mark_succeeded(old_job, chunk_count=1)

    async with file_doc_db.session() as session:
        row = (
            await session.execute(
                text(
                    "SELECT status, content_hash FROM knowledge_index_states "
                    "WHERE lower(replace(CAST(file_id AS TEXT), '-', ''))=:id_hex"
                ),
                {"id_hex": file_id.hex.lower()},
            )
        ).one()

    assert row._mapping["status"] == KnowledgeIndexStatus.EXTRACTING.value
    assert row._mapping["content_hash"] == "hash-v2"


@pytest.mark.asyncio
async def test_knowledge_search_applies_space_membership_acl(
    file_doc_db: DatabaseService,
    tmp_path: Path,
) -> None:
    """A space member can retrieve shared docs; a removed member cannot."""
    owner_id = uuid4()
    viewer_id = uuid4()
    workspace_id = uuid4()
    space_id = uuid4()
    file_id = uuid4()
    await _insert_dashed_file(
        file_doc_db,
        file_id=file_id,
        user_id=owner_id,
        name="shared.txt",
        storage_path=tmp_path / "shared.txt",
        extracted_text="space-only alpha evidence",
        library_scope="knowledge",
    )
    now = datetime.utcnow().isoformat(sep=" ")
    async with file_doc_db.session() as session:
        await session.execute(
            text("INSERT OR IGNORE INTO users (id, role, disabled) VALUES (:id, 'user', 0)"),
            {"id": str(viewer_id)},
        )
        await session.execute(
            text("INSERT INTO workspaces (id) VALUES (:id)"),
            {"id": str(workspace_id)},
        )
        for member_id, member_user in ((uuid4(), owner_id), (uuid4(), viewer_id)):
            await session.execute(
                text(
                    "INSERT INTO workspace_members "
                    "(id, created_at, updated_at, workspace_id, user_id, role) "
                    "VALUES (:id, :now, :now, :workspace_id, :user_id, 'member')"
                ),
                {
                    "id": str(member_id),
                    "now": now,
                    "workspace_id": str(workspace_id),
                    "user_id": str(member_user),
                },
            )
        await session.execute(
            text(
                "INSERT INTO knowledge_spaces "
                "(id, created_at, updated_at, workspace_id, name, slug, "
                "default_access_scope, is_default, is_deleted) "
                "VALUES (:id, :now, :now, :workspace_id, 'Shared', 'shared', 'space', 0, 0)"
            ),
            {"id": str(space_id), "now": now, "workspace_id": str(workspace_id)},
        )
        await session.execute(
            text(
                "INSERT INTO knowledge_space_members "
                "(id, created_at, updated_at, space_id, user_id, role) "
                "VALUES (:id, :now, :now, :space_id, :user_id, 'viewer')"
            ),
            {
                "id": str(uuid4()),
                "now": now,
                "space_id": str(space_id),
                "user_id": str(viewer_id),
            },
        )
        await session.execute(
            text(
                "UPDATE files SET workspace_id=:workspace_id, knowledge_space_id=:space_id, "
                "access_scope='space' WHERE id=:file_id"
            ),
            {
                "workspace_id": str(workspace_id),
                "space_id": str(space_id),
                "file_id": str(file_id),
            },
        )

    await file_doc_db.repositories.document_chunks.replace_for_file(
        file_id,
        owner_id,
        chunk_auto("space-only alpha evidence", mime_type="text/plain", filename="shared.txt"),
    )

    visible = await file_doc_db.repositories.document_chunks.search(
        viewer_id,
        "alpha",
        library_scope="knowledge",
    )
    assert [hit.file_id for hit in visible] == [file_id]

    async with file_doc_db.session() as session:
        await session.execute(
            text(
                "DELETE FROM knowledge_space_members "
                "WHERE lower(replace(CAST(space_id AS TEXT), '-', ''))=:space_hex "
                "AND lower(replace(CAST(user_id AS TEXT), '-', ''))=:user_hex"
            ),
            {"space_hex": space_id.hex.lower(), "user_hex": viewer_id.hex.lower()},
        )

    assert not await file_doc_db.repositories.document_chunks.search(
        viewer_id,
        "alpha",
        library_scope="knowledge",
    )


@pytest.mark.asyncio
async def test_knowledge_space_member_can_preview_shared_file(
    file_doc_db: DatabaseService,
    tmp_path: Path,
) -> None:
    """A reader who can discover a shared document can retrieve its bytes."""
    owner_id = uuid4()
    viewer_id = uuid4()
    stranger_id = uuid4()
    workspace_id = uuid4()
    space_id = uuid4()
    file_id = uuid4()
    await _insert_dashed_file(
        file_doc_db,
        file_id=file_id,
        user_id=owner_id,
        name="shared-preview.txt",
        storage_path=tmp_path / "shared-preview.txt",
        library_scope="knowledge",
    )
    now = datetime.utcnow().isoformat(sep=" ")
    async with file_doc_db.session() as session:
        for user_id in (viewer_id, stranger_id):
            await session.execute(
                text("INSERT OR IGNORE INTO users (id, role, disabled) VALUES (:id, 'user', 0)"),
                {"id": str(user_id)},
            )
        await session.execute(text("INSERT INTO workspaces (id) VALUES (:id)"), {"id": str(workspace_id)})
        for member_id, member_user in ((uuid4(), owner_id), (uuid4(), viewer_id)):
            await session.execute(
                text(
                    "INSERT INTO workspace_members "
                    "(id, created_at, updated_at, workspace_id, user_id, role) "
                    "VALUES (:id, :now, :now, :workspace_id, :user_id, 'member')"
                ),
                {
                    "id": str(member_id),
                    "now": now,
                    "workspace_id": str(workspace_id),
                    "user_id": str(member_user),
                },
            )
        await session.execute(
            text(
                "INSERT INTO knowledge_spaces "
                "(id, created_at, updated_at, workspace_id, name, slug, "
                "default_access_scope, is_default, is_deleted) "
                "VALUES (:id, :now, :now, :workspace_id, 'Shared previews', 'shared-previews', 'space', 0, 0)"
            ),
            {"id": str(space_id), "now": now, "workspace_id": str(workspace_id)},
        )
        await session.execute(
            text(
                "INSERT INTO knowledge_space_members "
                "(id, created_at, updated_at, space_id, user_id, role) "
                "VALUES (:id, :now, :now, :space_id, :user_id, 'viewer')"
            ),
            {
                "id": str(uuid4()),
                "now": now,
                "space_id": str(space_id),
                "user_id": str(viewer_id),
            },
        )
        await session.execute(
            text(
                "UPDATE files SET workspace_id=:workspace_id, knowledge_space_id=:space_id, "
                "access_scope='space' WHERE id=:file_id"
            ),
            {
                "workspace_id": str(workspace_id),
                "space_id": str(space_id),
                "file_id": str(file_id),
            },
        )

    shared = await _resolve_file_for_serve(
        file_id,
        token=None,
        jwt_user_id=viewer_id,
        required_scope="preview",
        db=file_doc_db,
    )
    assert shared.id == file_id

    with pytest.raises(HTTPException) as exc_info:
        await _resolve_file_for_serve(
            file_id,
            token=None,
            jwt_user_id=stranger_id,
            required_scope="download",
            db=file_doc_db,
        )
    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_personal_knowledge_space_bootstrap_is_idempotent(
    file_doc_db: DatabaseService,
) -> None:
    user_id = uuid4()
    async with file_doc_db.session() as session:
        await session.execute(
            text("INSERT OR IGNORE INTO users (id, role, disabled) VALUES (:id, 'user', 0)"),
            {"id": str(user_id)},
        )

    first = await ensure_personal_knowledge_space(file_doc_db, user_id)
    second = await ensure_personal_knowledge_space(file_doc_db, user_id)

    async with file_doc_db.session() as session:
        counts = (
            await session.execute(
                text(
                    "SELECT "
                    "(SELECT COUNT(*) FROM workspaces WHERE lower(CAST(id AS TEXT)) IN (:dashed, :hex)) AS workspaces, "
                    "(SELECT COUNT(*) FROM workspace_members WHERE lower(replace(CAST(user_id AS TEXT), '-', ''))=:hex) AS org_members, "
                    "(SELECT COUNT(*) FROM knowledge_spaces WHERE slug='personal' AND lower(replace(CAST(workspace_id AS TEXT), '-', ''))=:hex) AS spaces, "
                    "(SELECT COUNT(*) FROM knowledge_space_members WHERE lower(replace(CAST(space_id AS TEXT), '-', ''))=:space_hex) AS space_members"
                ),
                {
                    "dashed": str(user_id).lower(),
                    "hex": user_id.hex.lower(),
                    "space_hex": first.hex.lower(),
                },
            )
        ).one()

    assert first == second
    assert counts._mapping["workspaces"] == 1
    assert counts._mapping["org_members"] == 1
    assert counts._mapping["spaces"] == 1
    assert counts._mapping["space_members"] == 1


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
        "StAgent.services.service_manager.get_service_manager",
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
        "StAgent.services.service_manager.get_service_manager",
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

    monkeypatch.setattr("StAgent.tasks.jobs.enqueue_file_processing", _enqueue)
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
        "StAgent.tasks.jobs.enqueue_file_processing",
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
