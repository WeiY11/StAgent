"""Contract tests for the local sqlite-vec knowledge projection."""

from __future__ import annotations

from uuid import uuid4

import pytest

from leagent.library.chunking import Chunk


@pytest.mark.asyncio
async def test_sqlite_vec_index_persists_versioned_acl_filtered_vectors(tmp_path) -> None:
    """Only vectors belonging to the caller's allowed files are searchable."""
    from leagent.knowledge.sqlite_vec_index import SqliteVecKnowledgeIndex

    first_file = uuid4()
    second_file = uuid4()
    first_chunk = uuid4()
    second_chunk = uuid4()
    index = SqliteVecKnowledgeIndex(tmp_path, dimension=3, index_version="model-a")

    assert index.available
    assert await index.upsert_vectors(
        first_file,
        [(first_chunk, [1.0, 0.0, 0.0])],
    ) == 1
    assert await index.upsert_vectors(
        second_file,
        [(second_chunk, [0.0, 1.0, 0.0])],
    ) == 1
    assert await index.search(
        [1.0, 0.0, 0.0], limit=5, allowed_file_ids=[first_file]
    ) == [(str(first_chunk), pytest.approx(1.0))]

    restarted = SqliteVecKnowledgeIndex(tmp_path, dimension=3, index_version="model-a")
    assert await restarted.search(
        [0.0, 1.0, 0.0], limit=5, allowed_file_ids=[second_file]
    ) == [(str(second_chunk), pytest.approx(1.0))]


@pytest.mark.asyncio
async def test_sqlite_vec_index_keeps_candidate_versions_isolated_and_discardable(tmp_path) -> None:
    """Candidate versions neither leak into the active table nor survive discard."""
    from leagent.knowledge.sqlite_vec_index import SqliteVecKnowledgeIndex

    file_id = uuid4()
    active_chunk = uuid4()
    candidate_chunk = uuid4()
    index = SqliteVecKnowledgeIndex(tmp_path, dimension=2, index_version="active")
    await index.upsert_vectors(file_id, [(active_chunk, [1.0, 0.0])])
    await index.begin_version("candidate")
    await index.upsert_vectors(file_id, [(candidate_chunk, [0.0, 1.0])], index_version="candidate")

    assert [hit[0] for hit in await index.search([1.0, 0.0], limit=3, allowed_file_ids=[file_id])] == [str(active_chunk)]
    assert [hit[0] for hit in await index.search([0.0, 1.0], limit=3, allowed_file_ids=[file_id], index_version="candidate")] == [str(candidate_chunk)]
    assert await index.discard_version("candidate") is True
    assert await index.begin_version("candidate") is True
    await index.upsert_vectors(file_id, [(candidate_chunk, [0.0, 1.0])], index_version="candidate")
    assert await index.activate_version("candidate") is True
    restarted = SqliteVecKnowledgeIndex(tmp_path, dimension=2)
    assert restarted.index_version == "candidate"
    assert await restarted.search([0.0, 1.0], limit=3, allowed_file_ids=[file_id]) == [(str(candidate_chunk), pytest.approx(1.0))]


@pytest.mark.asyncio
async def test_sqlite_vec_index_dual_writes_chunks_and_persists_candidate_state(tmp_path) -> None:
    """A candidate receives explicit-version writes without changing the active table."""
    from leagent.knowledge.sqlite_vec_index import SqliteVecKnowledgeIndex

    class _Embeddings:
        async def embed(self, texts: list[str]) -> list[list[float]]:
            return [[1.0, 0.0] if text == "active" else [0.0, 1.0] for text in texts]

    file_id = uuid4()
    active_id = uuid4()
    candidate_id = uuid4()
    index = SqliteVecKnowledgeIndex(tmp_path, dimension=2, embeddings=_Embeddings(), index_version="active")
    assert await index.begin_version("candidate") is True
    assert await index.upsert_chunks(
        file_id, None, [Chunk(0, 0, 6, "active")], chunk_ids=[active_id]
    ) == 1
    assert await index.upsert_chunks(
        file_id,
        None,
        [Chunk(0, 0, 9, "candidate")],
        chunk_ids=[candidate_id],
        index_version="candidate",
    ) == 1

    assert await index.search([1.0, 0.0], limit=2, allowed_file_ids=[file_id]) == [(str(active_id), pytest.approx(1.0))]
    assert await index.search([0.0, 1.0], limit=2, allowed_file_ids=[file_id], index_version="candidate") == [(str(candidate_id), pytest.approx(1.0))]
    assert (await index.version_status())["candidate"] == "candidate"
    assert (await SqliteVecKnowledgeIndex(tmp_path, dimension=2).version_status())["candidate"] == "candidate"


@pytest.mark.asyncio
async def test_sqlite_vec_search_batches_more_than_sqlite_parameter_limit_without_read_ddl(tmp_path) -> None:
    """ACL metadata batching works and a read path never attempts table creation."""
    from leagent.knowledge.sqlite_vec_index import SqliteVecKnowledgeIndex

    file_id = uuid4()
    chunk_id = uuid4()
    index = SqliteVecKnowledgeIndex(tmp_path, dimension=2)
    await index.upsert_vectors(file_id, [(chunk_id, [1.0, 0.0])])
    index._ensure_table = lambda *_args: (_ for _ in ()).throw(AssertionError("read DDL"))  # type: ignore[method-assign]
    allowed = [uuid4() for _ in range(800)] + [file_id]
    assert await index.search([1.0, 0.0], limit=1, allowed_file_ids=allowed) == [(str(chunk_id), pytest.approx(1.0))]


@pytest.mark.asyncio
async def test_sqlite_vec_index_delete_supports_long_file_metadata_and_corruption_degrades(tmp_path) -> None:
    """Deletion is file-scoped and an unreadable sidecar falls back to no hits."""
    from leagent.knowledge.sqlite_vec_index import SqliteVecKnowledgeIndex

    file_id = uuid4()
    chunk_id = uuid4()
    index = SqliteVecKnowledgeIndex(tmp_path, dimension=2)
    await index.upsert_vectors(file_id, [(chunk_id, [1.0, 0.0])])
    assert await index.delete_for_file(file_id) == 1
    assert await index.search([1.0, 0.0], limit=1, allowed_file_ids=[file_id]) == []

    index.path.write_bytes(b"not a sqlite database")
    damaged = SqliteVecKnowledgeIndex(tmp_path, dimension=2)
    assert not damaged.available
    assert await damaged.search([1.0, 0.0], limit=1, allowed_file_ids=[file_id]) == []
