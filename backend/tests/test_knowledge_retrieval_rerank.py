"""Regression coverage for bounded knowledge-retrieval rerank candidates."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text

from StAgent.config.settings import Settings
from StAgent.db.repositories.document_chunks import ChunkHit
from StAgent.db.service import DatabaseService
from StAgent.knowledge.evaluation import RetrievalEvaluationCase, evaluate_retriever
from StAgent.knowledge.rerank import LexicalRerankProvider
from StAgent.library.chunking import Chunk
from StAgent.library.retrieval import KnowledgeRetrievalPipeline, SearchOptions

if TYPE_CHECKING:
    from pathlib import Path


@pytest_asyncio.fixture()
async def knowledge_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Create an isolated SQLite database with one visible knowledge file."""

    db_path = tmp_path / "knowledge-rerank.db"
    monkeypatch.setenv("DB_DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    db = DatabaseService(Settings())
    await db.create_tables()
    try:
        yield db
    finally:
        await db.dispose()


async def _insert_visible_knowledge_file(
    db: DatabaseService,
    *,
    file_id: UUID,
    user_id: UUID,
    file_type: str = "document",
    folder_id: UUID | None = None,
) -> None:
    now = datetime.utcnow().isoformat(sep=" ")
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
                "has_ocr, is_indexed, library_scope, folder_id, inbox_state, is_pinned) "
                "VALUES (0, :now, :now, :id, 'knowledge.txt', 'knowledge.txt', "
                ":file_type, 1, 'processed', :user_id, 'knowledge.txt', 'evidence', "
                "0, 1, 'knowledge', :folder_id, 'new', 0)"
            ),
            {
                "now": now,
                "id": str(file_id),
                "user_id": str(user_id),
                "file_type": file_type,
                "folder_id": str(folder_id) if folder_id else None,
            },
        )


async def _insert_folder(db: DatabaseService, *, folder_id: UUID, user_id: UUID) -> None:
    now = datetime.utcnow().isoformat(sep=" ")
    async with db.session() as session:
        await session.execute(
            text("INSERT OR IGNORE INTO users (id, role, disabled) VALUES (:id, 'user', 0)"),
            {"id": str(user_id)},
        )
        await session.execute(
            text(
                "INSERT INTO folders "
                "(is_deleted, created_at, updated_at, id, name, user_id, parent_id, "
                "position, file_count, flow_count, is_project) "
                "VALUES (0, :now, :now, :id, 'knowledge-folder', :user_id, NULL, 0, 0, 0, 0)"
            ),
            {"now": now, "id": str(folder_id), "user_id": str(user_id)},
        )


def _hit(file_id: UUID, chunk_id: UUID, *, rank: int, text_value: str) -> ChunkHit:
    return ChunkHit(
        chunk_id=chunk_id,
        file_id=file_id,
        file_name="knowledge.txt",
        seq=rank,
        start_offset=rank * 100,
        end_offset=rank * 100 + len(text_value),
        text=text_value,
        snippet=text_value,
        score=float(10 - rank),
    )


class _CandidateRepo:
    def __init__(self, hits: list[ChunkHit]) -> None:
        self._hits = hits
        self.last_search_kwargs: dict[str, object] = {}

    async def search(self, *_args: object, **kwargs: object) -> list[ChunkHit]:
        self.last_search_kwargs = dict(kwargs)
        return self._hits


class _WrappedDatabase:
    def __init__(self, db: DatabaseService, hits: list[ChunkHit]) -> None:
        self._db = db
        self.repositories = SimpleNamespace(document_chunks=_CandidateRepo(hits))

    def session(self):
        return self._db.session()


@pytest.mark.asyncio
async def test_pipeline_pushes_file_filters_to_chunk_candidate_query(
    knowledge_db: DatabaseService,
) -> None:
    """Space/folder/type selection must happen before the lexical candidate limit."""

    wrapped = _WrappedDatabase(knowledge_db, [])
    pipeline = KnowledgeRetrievalPipeline(wrapped, reranker=None)  # type: ignore[arg-type]
    space_id = uuid4()
    folder_id = uuid4()

    assert (
        await pipeline.search(
            SearchOptions(
                query="candidate",
                user_id=uuid4(),
                limit=1,
                space_id=space_id,
                folder_id=folder_id,
                file_type_filter=["document"],
                rerank=False,
            )
        )
        == []
    )
    assert (
        wrapped.repositories.document_chunks.last_search_kwargs.items()
        >= {
            "space_id": space_id,
            "folder_id": folder_id,
            "file_type_filter": ["document"],
        }.items()
    )


class _PromotingReranker:
    def __init__(self, target: str) -> None:
        self._target = target
        self.documents: list[str] = []

    async def rerank(
        self,
        _query: str,
        documents: list[str],
        *,
        top_n: int,
    ) -> list[tuple[int, float]]:
        self.documents = list(documents)
        try:
            index = self.documents.index(self._target)
        except ValueError:
            return []
        return [(index, 1.0)][:top_n]


class _RecordingLexicalReranker:
    def __init__(self) -> None:
        self._delegate = LexicalRerankProvider()
        self.documents: list[str] = []

    async def rerank(
        self,
        query: str,
        documents: list[str],
        *,
        top_n: int,
    ) -> list[tuple[int, float]]:
        self.documents = list(documents)
        return await self._delegate.rerank(query, documents, top_n=top_n)


class _FailingReranker:
    async def rerank(
        self,
        _query: str,
        _documents: list[str],
        *,
        top_n: int,
    ) -> list[tuple[int, float]]:
        del top_n
        raise RuntimeError("reranker unavailable")


class _VectorIndex:
    available = True
    requires_allowed_file_ids = False

    def __init__(self, *chunk_ids: UUID) -> None:
        self._chunk_ids = chunk_ids

    async def search(self, *_args: object, **_kwargs: object) -> list[tuple[str, float]]:
        return [(str(chunk_id), 1.0) for chunk_id in self._chunk_ids]


@pytest.mark.asyncio
async def test_reranker_promotes_relevant_candidate_beyond_initial_top_k(
    knowledge_db: DatabaseService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A relevant candidate at K+1 must reach the bounded rerank pool."""

    monkeypatch.setattr(
        "StAgent.knowledge.vector_index.get_knowledge_vector_index",
        lambda: None,
    )
    user_id = uuid4()
    file_id = uuid4()
    relevant_id = uuid4()
    relevant_text = "high-value evidence for the answer"
    await _insert_visible_knowledge_file(knowledge_db, file_id=file_id, user_id=user_id)
    hits = [
        _hit(file_id, uuid4(), rank=1, text_value="first lexical candidate"),
        _hit(file_id, uuid4(), rank=2, text_value="second lexical candidate"),
        _hit(file_id, relevant_id, rank=3, text_value=relevant_text),
        _hit(file_id, uuid4(), rank=4, text_value="fourth lexical candidate"),
        _hit(file_id, uuid4(), rank=5, text_value="fifth lexical candidate"),
        _hit(file_id, uuid4(), rank=6, text_value="sixth lexical candidate"),
        _hit(file_id, uuid4(), rank=7, text_value="seventh lexical candidate"),
        _hit(file_id, uuid4(), rank=8, text_value="eighth lexical candidate"),
    ]
    reranker = _RecordingLexicalReranker()
    pipeline = KnowledgeRetrievalPipeline(
        _WrappedDatabase(knowledge_db, hits),  # type: ignore[arg-type]
        reranker=reranker,
    )

    results = await pipeline.search(
        SearchOptions(
            query="answer evidence",
            user_id=user_id,
            limit=2,
            query_rewrite=False,
            rerank=True,
        )
    )

    assert len(reranker.documents) == 8
    assert [result.chunk_id for result in results] == [relevant_id, hits[0].chunk_id]


@pytest.mark.asyncio
async def test_reranker_can_promote_vector_only_candidate_when_lexical_top_k_is_full(
    knowledge_db: DatabaseService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A vector-only candidate must compete even when lexical results fill K."""

    user_id = uuid4()
    file_id = uuid4()
    relevant_text = "semantic evidence that answers the question"
    await _insert_visible_knowledge_file(knowledge_db, file_id=file_id, user_id=user_id)
    await knowledge_db.repositories.document_chunks.replace_for_file(
        file_id,
        user_id,
        [Chunk(seq=99, start_offset=0, end_offset=len(relevant_text), text=relevant_text)],
    )
    relevant_id = (await knowledge_db.repositories.document_chunks.list_ids_for_file(file_id))[0]
    monkeypatch.setattr(
        "StAgent.knowledge.vector_index.get_knowledge_vector_index",
        lambda: _VectorIndex(relevant_id),
    )
    hits = [
        _hit(file_id, uuid4(), rank=1, text_value="first lexical candidate"),
        _hit(file_id, uuid4(), rank=2, text_value="second lexical candidate"),
    ]
    reranker = _PromotingReranker(relevant_text)
    pipeline = KnowledgeRetrievalPipeline(
        _WrappedDatabase(knowledge_db, hits),  # type: ignore[arg-type]
        reranker=reranker,
    )

    results = await pipeline.search(
        SearchOptions(
            query="answer evidence",
            user_id=user_id,
            limit=2,
            query_rewrite=False,
            rerank=True,
        )
    )

    assert relevant_text in reranker.documents
    assert results[0].chunk_id == relevant_id


@pytest.mark.asyncio
async def test_vector_only_candidates_respect_folder_and_type_filters(
    knowledge_db: DatabaseService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Vector-only candidates must use the same folder/type filter as lexical hits."""

    user_id = uuid4()
    target_folder_id, other_folder_id = uuid4(), uuid4()
    await _insert_folder(knowledge_db, folder_id=target_folder_id, user_id=user_id)
    await _insert_folder(knowledge_db, folder_id=other_folder_id, user_id=user_id)

    allowed_file_id, wrong_type_file_id, wrong_folder_file_id = uuid4(), uuid4(), uuid4()
    await _insert_visible_knowledge_file(
        knowledge_db,
        file_id=allowed_file_id,
        user_id=user_id,
        folder_id=target_folder_id,
    )
    await _insert_visible_knowledge_file(
        knowledge_db,
        file_id=wrong_type_file_id,
        user_id=user_id,
        file_type="image",
        folder_id=target_folder_id,
    )
    await _insert_visible_knowledge_file(
        knowledge_db,
        file_id=wrong_folder_file_id,
        user_id=user_id,
        folder_id=other_folder_id,
    )

    wrong_type_text = "image-only vector candidate"
    wrong_folder_text = "outside-folder vector candidate"
    await knowledge_db.repositories.document_chunks.replace_for_file(
        wrong_type_file_id,
        user_id,
        [Chunk(seq=1, start_offset=0, end_offset=len(wrong_type_text), text=wrong_type_text)],
    )
    await knowledge_db.repositories.document_chunks.replace_for_file(
        wrong_folder_file_id,
        user_id,
        [Chunk(seq=1, start_offset=0, end_offset=len(wrong_folder_text), text=wrong_folder_text)],
    )
    wrong_type_id = (await knowledge_db.repositories.document_chunks.list_ids_for_file(wrong_type_file_id))[0]
    wrong_folder_id = (await knowledge_db.repositories.document_chunks.list_ids_for_file(wrong_folder_file_id))[0]
    monkeypatch.setattr(
        "StAgent.knowledge.vector_index.get_knowledge_vector_index",
        lambda: _VectorIndex(wrong_type_id, wrong_folder_id),
    )

    lexical_id = uuid4()
    reranker = _RecordingLexicalReranker()
    pipeline = KnowledgeRetrievalPipeline(
        _WrappedDatabase(
            knowledge_db,
            [_hit(allowed_file_id, lexical_id, rank=1, text_value="allowed lexical candidate")],
        ),  # type: ignore[arg-type]
        reranker=reranker,
    )

    results = await pipeline.search(
        SearchOptions(
            query="candidate",
            user_id=user_id,
            limit=1,
            file_type_filter=["document"],
            folder_id=target_folder_id,
            query_rewrite=False,
            rerank=True,
        )
    )

    assert [result.chunk_id for result in results] == [lexical_id]
    assert wrong_type_text not in reranker.documents
    assert wrong_folder_text not in reranker.documents


@pytest.mark.asyncio
async def test_reranker_failure_still_returns_only_the_requested_top_k(
    knowledge_db: DatabaseService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed reranker degrades to the fused top-K, never its candidate pool."""

    monkeypatch.setattr(
        "StAgent.knowledge.vector_index.get_knowledge_vector_index",
        lambda: None,
    )
    user_id = uuid4()
    file_id = uuid4()
    await _insert_visible_knowledge_file(knowledge_db, file_id=file_id, user_id=user_id)
    hits = [_hit(file_id, uuid4(), rank=rank, text_value=f"candidate {rank}") for rank in range(1, 9)]
    pipeline = KnowledgeRetrievalPipeline(
        _WrappedDatabase(knowledge_db, hits),  # type: ignore[arg-type]
        reranker=_FailingReranker(),
    )

    results = await pipeline.search(
        SearchOptions(
            query="candidate",
            user_id=user_id,
            limit=2,
            query_rewrite=False,
            rerank=True,
        )
    )

    assert len(results) == 2
    assert [result.chunk_id for result in results] == [hits[0].chunk_id, hits[1].chunk_id]


@pytest.mark.asyncio
async def test_candidate_pool_lifts_relevant_chunks_into_recall_at_ten(
    knowledge_db: DatabaseService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Relevant candidates beyond lexical rank ten must affect top-ten metrics."""

    monkeypatch.setattr(
        "StAgent.knowledge.vector_index.get_knowledge_vector_index",
        lambda: None,
    )
    user_id = uuid4()
    file_id = uuid4()
    await _insert_visible_knowledge_file(knowledge_db, file_id=file_id, user_id=user_id)
    hits = [
        _hit(
            file_id,
            uuid4(),
            rank=rank,
            text_value=f"edge platform background candidate {rank}",
        )
        for rank in range(1, 11)
    ]
    relevant_ids: set[UUID] = set()
    for rank in range(11, 14):
        chunk_id = uuid4()
        relevant_ids.add(chunk_id)
        hits.append(
            _hit(
                file_id,
                chunk_id,
                rank=rank,
                text_value=f"edge latency evidence candidate {rank}",
            )
        )
    reranker = _RecordingLexicalReranker()
    pipeline = KnowledgeRetrievalPipeline(
        _WrappedDatabase(knowledge_db, hits),  # type: ignore[arg-type]
        reranker=reranker,
    )

    async def retrieve(query: str, k: int):
        return await pipeline.search(
            SearchOptions(
                query=query,
                user_id=user_id,
                limit=k,
                query_rewrite=False,
                rerank=True,
            )
        )

    report = await evaluate_retriever(
        [
            RetrievalEvaluationCase(
                case_id="late-relevant-candidates",
                query="edge latency",
                relevant_chunk_ids=frozenset(str(chunk_id) for chunk_id in relevant_ids),
            )
        ],
        retrieve,
        k=10,
        recall_target=1.0,
        ndcg_target=1.0,
        p95_ms_target=5000.0,
    )

    assert len(reranker.documents) == 13
    assert report.recall_at_k == 1.0
    assert report.precision_at_k == pytest.approx(0.30)
    assert report.ndcg_at_k == 1.0
    assert report.mrr == 1.0


@pytest.mark.asyncio
async def test_duplicate_lexical_chunk_does_not_consume_a_top_k_slot(
    knowledge_db: DatabaseService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same chunk from two lexical sources cannot crowd out another hit."""

    monkeypatch.setattr(
        "StAgent.knowledge.vector_index.get_knowledge_vector_index",
        lambda: None,
    )
    user_id = uuid4()
    file_id = uuid4()
    duplicate_id = uuid4()
    relevant_id = uuid4()
    await _insert_visible_knowledge_file(knowledge_db, file_id=file_id, user_id=user_id)
    hits = [
        _hit(file_id, duplicate_id, rank=1, text_value="repeated lexical candidate"),
        _hit(file_id, duplicate_id, rank=2, text_value="repeated lexical candidate"),
        _hit(file_id, relevant_id, rank=3, text_value="another relevant candidate"),
    ]
    pipeline = KnowledgeRetrievalPipeline(
        _WrappedDatabase(knowledge_db, hits),  # type: ignore[arg-type]
        reranker=LexicalRerankProvider(),
    )

    results = await pipeline.search(
        SearchOptions(
            query="candidate",
            user_id=user_id,
            limit=2,
            query_rewrite=False,
            rerank=False,
        )
    )

    assert [result.chunk_id for result in results] == [duplicate_id, relevant_id]
