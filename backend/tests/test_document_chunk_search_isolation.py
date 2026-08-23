"""Regression tests for document chunk search ownership checks."""

from __future__ import annotations

import tempfile
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from StAgent.api.v1.documents import search_documents
from StAgent.api.v1.files import search_files
from StAgent.config.settings import Settings
from StAgent.db.models.file import File, FileStatus, FileType, LibraryScope
from StAgent.db.models.identity_stub import UserStub
from StAgent.db.repositories.document_chunks import ChunkHit
from StAgent.db.service import DatabaseService
from StAgent.library.chunking import chunk_text


@pytest.fixture()
async def search_db(monkeypatch: pytest.MonkeyPatch):
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "search.db"
        monkeypatch.setenv("DB_DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
        db = DatabaseService(Settings())
        await db.create_tables()
        yield db
        await db.dispose()


async def _add_file(
    db: DatabaseService,
    *,
    file_id: UUID,
    user_id: UUID,
    name: str,
    text: str,
) -> None:
    async with db.session() as session:
        session.add(UserStub(id=user_id))
    async with db.session() as session:
        session.add(
            File(
                id=file_id,
                user_id=user_id,
                name=name,
                original_name=name,
                file_type=FileType.DOCUMENT,
                size=len(text),
                status=FileStatus.PROCESSED,
                storage_path=f"/tmp/{name}",
                extracted_text=text,
                is_indexed=True,
                library_scope=LibraryScope.KNOWLEDGE,
            )
        )


@pytest.mark.asyncio
async def test_chunk_search_requires_chunk_and_file_to_have_same_owner(
    search_db: DatabaseService,
) -> None:
    owner = uuid4()
    other = uuid4()
    leaked_file_id = uuid4()
    text = "Secret acquisition plan should not appear in another user's results."

    await _add_file(
        search_db,
        file_id=leaked_file_id,
        user_id=other,
        name="other-secret.md",
        text=text,
    )

    await search_db.repositories.document_chunks.replace_for_file(
        leaked_file_id,
        owner,
        chunk_text(text),
    )

    hits = await search_db.repositories.document_chunks.search(
        owner,
        "Secret",
        limit=10,
        library_scope=LibraryScope.KNOWLEDGE.value,
    )

    assert hits == []


class _EmptyExecResult:
    def all(self) -> list[object]:
        return []


class _FakeSession:
    def __init__(self, files: dict[UUID, File]) -> None:
        self._files = files

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def get(self, _model: object, file_id: UUID) -> File | None:
        return self._files.get(file_id)

    async def exec(self, _stmt: object) -> _EmptyExecResult:
        return _EmptyExecResult()


class _FakeChunkRepository:
    def __init__(self, hits: list[ChunkHit]) -> None:
        self._hits = hits

    async def search(self, *_args: object, **_kwargs: object) -> list[ChunkHit]:
        return self._hits


class _FakeFileRepository:
    def __init__(self, files: dict[UUID, File]) -> None:
        self._files = files

    async def get_many_for_user(
        self, file_ids: list[UUID], user_id: UUID
    ) -> list[File]:
        return [
            f
            for fid in file_ids
            if (f := self._files.get(fid)) is not None
            and str(f.user_id) == str(user_id)
        ]


class _FakeRepositories:
    def __init__(self, files: dict[UUID, File], hits: list[ChunkHit]) -> None:
        self.files = _FakeFileRepository(files)
        self.document_chunks = _FakeChunkRepository(hits)


class _FakeDatabaseService:
    def __init__(self, files: dict[UUID, File], hits: list[ChunkHit]) -> None:
        self.repositories = _FakeRepositories(files, hits)
        self._files = files

    def session(self) -> _FakeSession:
        return _FakeSession(self._files)


def _indexed_hit(file_id: UUID) -> ChunkHit:
    return ChunkHit(
        chunk_id=uuid4(),
        file_id=file_id,
        file_name="other-secret.md",
        seq=0,
        start_offset=0,
        end_offset=20,
        text="Secret acquisition",
        snippet="Secret acquisition",
        score=1.0,
    )


def _owned_file(*, file_id: UUID, user_id: UUID) -> File:
    return File(
        id=file_id,
        user_id=user_id,
        name="other-secret.md",
        original_name="other-secret.md",
        file_type=FileType.DOCUMENT,
        size=20,
        status=FileStatus.PROCESSED,
        storage_path="/tmp/other-secret.md",
        extracted_text="Secret acquisition",
        is_indexed=True,
        library_scope=LibraryScope.KNOWLEDGE,
    )


@pytest.mark.asyncio
async def test_file_search_rechecks_chunk_hit_file_owner() -> None:
    owner = uuid4()
    other = uuid4()
    file_id = uuid4()
    db = _FakeDatabaseService(
        {file_id: _owned_file(file_id=file_id, user_id=other)},
        [_indexed_hit(file_id)],
    )

    response = await search_files(
        owner,
        db,  # type: ignore[arg-type]
        query="Secret",
        library_scope=None,
        limit=10,
    )

    assert response.results == []
    assert response.total == 0


@pytest.mark.asyncio
async def test_document_search_rechecks_chunk_hit_file_owner() -> None:
    owner = uuid4()
    other = uuid4()
    file_id = uuid4()
    db = _FakeDatabaseService(
        {file_id: _owned_file(file_id=file_id, user_id=other)},
        [_indexed_hit(file_id)],
    )

    response = await search_documents(
        owner,
        db,  # type: ignore[arg-type]
        query="Secret",
        file_types=None,
        folder_id=None,
        limit=10,
    )

    assert response.results == []
    assert response.total == 0
