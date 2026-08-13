"""Unit coverage for the quality-profile RAG primitives."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from uuid import uuid4

import pytest

from leagent.knowledge.evaluation import RetrievalEvaluationCase, evaluate_retriever
from leagent.knowledge.evidence import EvidenceStatus, build_evidence
from leagent.knowledge.opensearch import OpenSearchKnowledgeIndex
from leagent.knowledge.query import DeterministicQueryRewriter
from leagent.knowledge.rerank import LexicalRerankProvider
from leagent.knowledge.vector_index import KnowledgeVectorIndex
from leagent.library.chunking import Chunk
from leagent.library.retrieval import (
    KnowledgeRetrievalPipeline,
    RetrievalResult,
    SearchOptions,
    _rrf_fusion_scores,
)


@pytest.mark.asyncio
async def test_query_rewrite_is_bounded_and_keeps_original() -> None:
    result = await DeterministicQueryRewriter().rewrite(
        "查找“向量检索”",
        conversation=[{"role": "user", "content": "知识库"}],
    )
    assert result.original == "查找“向量检索”"
    assert result.variants[0]
    assert len(result.variants) <= 4
    assert result.original in result.variants


@pytest.mark.asyncio
async def test_local_reranker_prefers_phrase_and_term_overlap() -> None:
    ranked = await LexicalRerankProvider().rerank(
        "Milvus 向量索引",
        ["无关文本", "Milvus 向量索引的版本与 ACL 过滤"],
        top_n=2,
    )
    assert ranked[0][0] == 1


def test_rrf_fuses_lexical_and_vector_ranks() -> None:
    lexical = type("Hit", (), {"chunk_id": uuid4()})()
    vector_id = str(uuid4())
    scores = _rrf_fusion_scores([lexical], {vector_id: 0.8})
    assert str(lexical.chunk_id) in scores
    assert vector_id in scores


@pytest.mark.asyncio
async def test_knowledge_vector_index_deletes_document_scope() -> None:
    """Document reindexing delegates stale-vector cleanup to Milvus."""
    file_id = uuid4()

    class _Collection:
        can_write = True

        async def delete_scope(self, scope: str) -> int:
            assert scope == str(file_id)
            return 4

    index = object.__new__(KnowledgeVectorIndex)
    index._collection = _Collection()  # noqa: SLF001 - isolate the index delegation contract

    assert await index.delete_for_file(file_id) == 4


@pytest.mark.asyncio
async def test_vector_index_degraded_paths_do_not_raise_when_logging_is_enabled(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Best-effort vector failures must not be masked by logging failures."""
    caplog.set_level(logging.DEBUG, logger="leagent.knowledge.vector_index")
    file_id = uuid4()

    class _EmbeddingFailure:
        dimension = 8

        async def embed(self, _texts: list[str]) -> list[list[float]]:
            raise RuntimeError("embedding backend unavailable")

    first = object.__new__(KnowledgeVectorIndex)
    first._embeddings = _EmbeddingFailure()  # noqa: SLF001 - isolate failure boundary
    first._collection = SimpleNamespace(can_write=True)  # noqa: SLF001

    assert (
        await first.upsert_chunks(
            file_id,
            None,
            [Chunk(seq=0, start_offset=0, end_offset=4, text="text")],
        )
        == 0
    )

    class _Embeddings:
        dimension = 8

        async def embed(self, _texts: list[str]) -> list[list[float]]:
            return [[1.0] * 8]

    class _CollectionFailure:
        can_write = True

        async def upsert(self, **_kwargs: object) -> object:
            raise RuntimeError("milvus unavailable")

        async def delete_scope(self, _scope: str) -> int:
            raise RuntimeError("milvus unavailable")

    second = object.__new__(KnowledgeVectorIndex)
    second._embeddings = _Embeddings()  # noqa: SLF001
    second._collection = _CollectionFailure()  # noqa: SLF001

    assert (
        await second.upsert_chunks(
            file_id,
            None,
            [Chunk(seq=0, start_offset=0, end_offset=4, text="text")],
        )
        == 0
    )
    assert await second.delete_for_file(file_id) == 0


def test_knowledge_vector_index_isolates_nondefault_index_versions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A new embedding/index version must not reuse the legacy collection."""
    import leagent.memory.vector

    names: list[str] = []

    class _Collection:
        def __init__(self, *, name: str, **_kwargs: object) -> None:
            names.append(name)

    monkeypatch.setattr(leagent.memory.vector, "MilvusCollection", _Collection)
    embeddings = SimpleNamespace(dimension=8)

    KnowledgeVectorIndex(embeddings=embeddings)
    KnowledgeVectorIndex(embeddings=embeddings, index_version="embedding-v2")

    assert names[0] == "knowledge_chunks"
    assert names[1].startswith("knowledge_chunks_embedding_v2_")
    assert names[1] != names[0]


def test_evidence_gate_marks_empty_retrieval_insufficient() -> None:
    empty = build_evidence([], strict=True)
    assert empty.status is EvidenceStatus.INSUFFICIENT


def test_evidence_gate_builds_stable_citation_ids() -> None:
    result = RetrievalResult(
        file_id=uuid4(),
        chunk_id=uuid4(),
        file_name="guide.md",
        score=0.4,
        snippet="grounded text",
        start_offset=10,
        end_offset=30,
        seq=1,
        text="grounded text",
    )
    bundle = build_evidence([result], strict=True)
    assert bundle.status is EvidenceStatus.SUFFICIENT
    assert bundle.citations[0].citation_id == "K1"


@pytest.mark.asyncio
async def test_opensearch_projection_writes_versioned_bulk_documents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx

    calls: list[tuple[str, dict[str, object]]] = []

    class _Response:
        def __init__(self, payload: dict[str, object]) -> None:
            self._payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return self._payload

    class _Client:
        def __init__(self, **_: object) -> None:
            return None

        async def __aenter__(self) -> _Client:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def post(self, url: str, **kwargs: object) -> _Response:
            calls.append((url, kwargs))
            if url.endswith("/_bulk"):
                return _Response({"errors": False, "items": [{"index": {"status": 201}}]})
            return _Response({"deleted": 2})

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    file_id = uuid4()
    chunk_id = uuid4()
    index = OpenSearchKnowledgeIndex("http://opensearch", api_key="secret")

    written = await index.upsert_chunks(
        file_id,
        "guide.md",
        [Chunk(seq=0, start_offset=0, end_offset=12, text="grounded text")],
        chunk_ids=[chunk_id],
        index_version="rag-v2",
    )
    deleted = await index.delete_for_file(file_id, index_version="rag-v2")

    assert written == 1
    assert deleted == 2
    assert calls[0][0] == "http://opensearch/_bulk"
    assert '"index_version":"rag-v2"' in str(calls[0][1]["content"])
    assert calls[1][0] == "http://opensearch/knowledge_chunks/_delete_by_query"


@pytest.mark.asyncio
async def test_opensearch_version_filters_cover_dynamic_keyword_mappings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Versioned retrieval must work before a dedicated index mapping exists."""
    import httpx

    calls: list[dict[str, object]] = []

    class _Response:
        def __init__(self, payload: dict[str, object]) -> None:
            self._payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return self._payload

    class _Client:
        def __init__(self, **_: object) -> None:
            return None

        async def __aenter__(self) -> _Client:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def post(self, _url: str, **kwargs: object) -> _Response:
            calls.append(kwargs)
            if "_search" in _url:
                return _Response({"hits": {"hits": []}})
            return _Response({"deleted": 1})

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    file_id = uuid4()
    index = OpenSearchKnowledgeIndex("http://opensearch")

    await index.search("cache", index_version="rag-v2")
    await index.delete_for_file(file_id, index_version="rag-v2")

    dynamic_version_filter = {
        "bool": {
            "should": [
                {"term": {"index_version": "rag-v2"}},
                {"term": {"index_version.keyword": "rag-v2"}},
            ],
            "minimum_should_match": 1,
        }
    }
    dynamic_file_filter = {
        "bool": {
            "should": [
                {"term": {"file_id": str(file_id)}},
                {"term": {"file_id.keyword": str(file_id)}},
            ],
            "minimum_should_match": 1,
        }
    }
    search_filters = calls[0]["json"]["query"]["bool"]["filter"]  # type: ignore[index]
    delete_filters = calls[1]["json"]["query"]["bool"]["filter"]  # type: ignore[index]
    assert search_filters == [dynamic_version_filter]
    assert delete_filters == [dynamic_file_filter, dynamic_version_filter]


@pytest.mark.asyncio
async def test_knowledge_pipeline_fails_closed_without_user_identity() -> None:
    class _NeverQueriedDb:
        repositories = object()

    pipeline = KnowledgeRetrievalPipeline(_NeverQueriedDb())  # type: ignore[arg-type]
    assert await pipeline.search(SearchOptions(query="secret", user_id=None)) == []


@pytest.mark.asyncio
async def test_retrieval_evaluation_reports_retrieval_gate_and_unproven_answer_gate() -> None:
    first, second = uuid4(), uuid4()

    async def retrieve(query: str, k: int) -> list[dict[str, str]]:
        return [{"chunk_id": str(first)}, {"chunk_id": str(second)}]

    report = await evaluate_retriever(
        [
            RetrievalEvaluationCase(
                query="acl",
                relevant_chunk_ids=frozenset({str(first)}),
            )
        ],
        retrieve,
        k=2,
        recall_target=1.0,
        ndcg_target=1.0,
        p95_ms_target=5000.0,
    )
    assert report.retrieval_gate_passed is True
    assert report.strict_gate_passed is False
    assert report.recall_at_k == 1.0
    assert report.ndcg_at_k == 1.0
