"""Vector cleanup coverage for knowledge garbage collection."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from leagent.knowledge.gc import KnowledgeGarbageCollector


@pytest.mark.asyncio
async def test_gc_summary_logging_does_not_raise_when_info_is_enabled(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """GC completion remains observable when application logging uses INFO."""
    collector = KnowledgeGarbageCollector(object())  # type: ignore[arg-type]
    monkeypatch.setattr(collector, "_collect_orphaned_chunks", AsyncMock(return_value=0))
    monkeypatch.setattr(collector, "_collect_orphaned_contents", AsyncMock(return_value=0))
    monkeypatch.setattr(collector, "_collect_stuck_processing", AsyncMock(return_value=0))
    monkeypatch.setattr(collector, "_collect_expired_files", AsyncMock(return_value=0))
    caplog.set_level(logging.INFO, logger="leagent.knowledge.gc")

    assert (await collector.collect()).to_dict()["errors"] == []


@pytest.mark.asyncio
async def test_orphaned_chunk_gc_removes_matching_vector_scopes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Soft-deleted knowledge documents must not retain vector replicas."""
    file_id = uuid4()

    class _Result:
        def __init__(self, *, rows: list[tuple[str]] | None = None, rowcount: int = 2) -> None:
            self._rows = rows or []
            self.rowcount = rowcount

        def fetchall(self) -> list[tuple[str]]:
            return self._rows

    class _Session:
        def __init__(self) -> None:
            self.calls = 0

        async def __aenter__(self) -> _Session:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def execute(self, _statement: object) -> _Result:
            self.calls += 1
            if self.calls == 1:
                return _Result(rows=[(str(file_id),)])
            return _Result(rowcount=2)

    session = _Session()

    class _Db:
        def session(self) -> _Session:
            return session

    deleted_scopes: list[str] = []

    class _VectorIndex:
        available = True

        async def delete_for_file(self, deleted_file_id):  # noqa: ANN001
            deleted_scopes.append(str(deleted_file_id))
            return 2

    monkeypatch.setattr(
        "leagent.knowledge.vector_index.get_knowledge_vector_index",
        lambda: _VectorIndex(),
    )

    collector = KnowledgeGarbageCollector(_Db())  # type: ignore[arg-type]

    assert await collector._collect_orphaned_chunks() == 2
    assert deleted_scopes == [str(file_id)]
