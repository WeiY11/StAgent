"""Unchanged-snapshot dedupe in ``TieredSessionStore.save``.

``locked()`` exits and ``get_or_create`` re-save states that did not change;
the store skips the durable blob UPDATE + message-existence scan when the
serialized snapshot is byte-identical to the last successful write.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from uuid import uuid4

import pytest

from StAgent.config.settings import Settings
from StAgent.db.service import DatabaseService
from StAgent.services.session.state import SessionState
from StAgent.services.session.store import TieredSessionStore


@pytest.fixture()
async def db(monkeypatch: pytest.MonkeyPatch):
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "dedupe.db"
        monkeypatch.setenv("DB_DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
        service = DatabaseService(Settings())
        await service.create_tables()
        yield service
        await service.dispose()


@pytest.mark.asyncio
async def test_unchanged_save_skips_durable_write(db: DatabaseService) -> None:
    store = TieredSessionStore(Settings(), database=db)
    state = SessionState(session_id=uuid4(), user_id=uuid4())

    calls: list[str] = []
    original = store._save_to_database

    async def counting_save(s, snapshot_json):
        calls.append(snapshot_json)
        await original(s, snapshot_json)

    store._save_to_database = counting_save  # type: ignore[method-assign]

    await store.save(state)
    assert len(calls) == 1

    # Unchanged state: skipped entirely.
    await store.save(state)
    assert len(calls) == 1

    # A real mutation re-persists.
    state.metadata = {"note": "changed"}
    await store.save(state)
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_delete_clears_digest_so_recreated_session_persists(
    db: DatabaseService,
) -> None:
    session_id = uuid4()
    store = TieredSessionStore(Settings(), database=db)
    state = SessionState(session_id=session_id, user_id=uuid4())

    await store.save(state)
    await store.delete(session_id)

    # Recreating an identical snapshot must not be suppressed by a stale digest.
    state2 = SessionState(session_id=session_id, user_id=uuid4())
    state2.created_at = state.created_at
    state2.updated_at = state.updated_at
    await store.save(state2)

    loaded = await store.load(session_id)
    assert loaded is not None
    assert loaded.metadata == state2.metadata


@pytest.mark.asyncio
async def test_failed_save_is_retried(db: DatabaseService) -> None:
    store = TieredSessionStore(Settings(), database=db)
    state = SessionState(session_id=uuid4(), user_id=uuid4())

    original = store._save_to_database
    once = {"failed": False}

    async def failing_save(s, snapshot_json):
        if not once["failed"]:
            once["failed"] = True
            raise RuntimeError("transient database error")
        await original(s, snapshot_json)

    store._save_to_database = failing_save  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        await store.save(state)

    # Digest never advanced on failure — retry goes through the durable path.
    await store.save(state)
    loaded = await store.load(state.session_id)
    assert loaded is not None
