"""SQLite dashed UUID compatibility for chat/session persistence paths."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text

from StAgent.config.settings import Settings
from StAgent.db.models.message import MessageRole, MessageStatus
from StAgent.db.service import DatabaseService
from StAgent.services.chat.repair import deduplicate_user_messages
from StAgent.services.chat.service import ChatService
from StAgent.services.session.state import SessionMessage, SessionState
from StAgent.services.session.store import TieredSessionStore


@pytest_asyncio.fixture()
async def chat_uuid_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    db_path = tmp_path / "chat-uuid.db"
    monkeypatch.setenv("DB_DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    db = DatabaseService(Settings())
    await db.create_tables()
    try:
        yield db
    finally:
        await db.dispose()


async def _insert_dashed_user_session(
    db: DatabaseService,
    *,
    user_id: UUID,
    session_id: UUID,
    message_count: int = 0,
) -> None:
    now = datetime.utcnow().isoformat(sep=" ")
    async with db.session() as session:
        await session.execute(
            text("INSERT INTO users (id, role, disabled) VALUES (:id, 'user', 0)"),
            {"id": str(user_id)},
        )
        await session.execute(
            text(
                """
                INSERT INTO chat_sessions (
                    created_at, updated_at, id, name, user_id, is_active,
                    message_count, last_message_at
                )
                VALUES (:now, :now, :id, 'dashed chat', :uid, 1, :count, :now)
                """
            ),
            {
                "now": now,
                "id": str(session_id),
                "uid": str(user_id),
                "count": message_count,
            },
        )


async def _insert_dashed_project(
    db: DatabaseService,
    *,
    user_id: UUID,
    project_id: UUID,
) -> None:
    now = datetime.utcnow().isoformat(sep=" ")
    async with db.session() as session:
        await session.execute(
            text("INSERT INTO users (id, role, disabled) VALUES (:id, 'user', 0)"),
            {"id": str(user_id)},
        )
        await session.execute(
            text(
                """
                INSERT INTO chat_projects (
                    is_deleted, created_at, updated_at, id, user_id, name
                )
                VALUES (0, :now, :now, :id, :uid, 'project')
                """
            ),
            {"now": now, "id": str(project_id), "uid": str(user_id)},
        )


async def _insert_dashed_user_and_flows(
    db: DatabaseService,
    *,
    user_id: UUID,
    flow_ids: list[UUID],
) -> None:
    now = datetime.utcnow().isoformat(sep=" ")
    async with db.session() as session:
        await session.execute(
            text("INSERT INTO users (id, role, disabled) VALUES (:id, 'user', 0)"),
            {"id": str(user_id)},
        )
        for flow_id in flow_ids:
            await session.execute(
                text(
                    """
                    INSERT INTO flows (
                        is_deleted, created_at, updated_at, id, name, status,
                        flow_type, is_public, version, user_id, run_count
                    )
                    VALUES (
                        0, :now, :now, :id, 'flow', 'draft',
                        'workflow', 0, 1, :uid, 0
                    )
                    """
                ),
                {
                    "now": now,
                    "id": str(flow_id),
                    "uid": str(user_id),
                },
            )


@pytest.mark.asyncio
async def test_chat_service_create_session_preserves_dashed_sqlite_parent_fks(
    chat_uuid_db: DatabaseService,
) -> None:
    user_id = uuid4()
    project_id = uuid4()
    await _insert_dashed_project(
        chat_uuid_db,
        user_id=user_id,
        project_id=project_id,
    )
    service = ChatService(Settings(), db_service=chat_uuid_db, cache_service=None)

    created = await service.create_session(
        user_id,
        name="project chat",
        project_id=project_id,
    )
    loaded = await service.get_session(created.id, user_id=user_id)

    async with chat_uuid_db.session() as session:
        row = (
            await session.execute(
                text(
                    """
                    SELECT CAST(id AS TEXT) AS id,
                           CAST(user_id AS TEXT) AS user_id,
                           CAST(project_id AS TEXT) AS project_id
                    FROM chat_sessions
                    WHERE lower(replace(CAST(id AS TEXT), '-', '')) = :sid
                    """
                ),
                {"sid": str(created.id).replace("-", "").lower()},
            )
        ).mappings().one()

    assert loaded is not None
    assert loaded.id == created.id
    assert row["id"] == created.id.hex
    assert row["user_id"] == str(user_id)
    assert row["project_id"] == str(project_id)


@pytest.mark.asyncio
async def test_chat_service_list_and_count_sessions_filter_by_flow_id(
    chat_uuid_db: DatabaseService,
) -> None:
    user_id = uuid4()
    flow_a = uuid4()
    flow_b = uuid4()
    await _insert_dashed_user_and_flows(
        chat_uuid_db,
        user_id=user_id,
        flow_ids=[flow_a, flow_b],
    )
    service = ChatService(Settings(), db_service=chat_uuid_db, cache_service=None)
    session_a = await service.create_session(user_id, name="A", flow_id=flow_a)
    await service.create_session(user_id, name="B", flow_id=flow_b)

    rows = await service.list_sessions(user_id, flow_id=flow_a)
    total = await service.count_sessions(user_id, flow_id=flow_a)

    assert [row.id for row in rows] == [session_a.id]
    assert total == 1


@pytest.mark.asyncio
async def test_chat_service_writes_and_reads_dashed_sqlite_session_rows(
    chat_uuid_db: DatabaseService,
) -> None:
    user_id = uuid4()
    session_id = uuid4()
    await _insert_dashed_user_session(
        chat_uuid_db,
        user_id=user_id,
        session_id=session_id,
    )
    service = ChatService(Settings(), db_service=chat_uuid_db, cache_service=None)

    user_msg = await service.add_message(
        session_id,
        MessageRole.USER,
        "hello",
        user_id=user_id,
    )
    assistant_msg = await service.add_message(
        session_id,
        MessageRole.ASSISTANT,
        "hi",
        user_id=user_id,
    )
    history = await service.get_history(session_id, user_id=user_id, limit=10)
    filtered = await service.filter_message_ids_for_session(
        session_id,
        [user_msg.id, uuid4()],
    )
    status_ok = await service.update_message_status(
        user_msg.id,
        MessageStatus.FAILED,
        error="boom",
    )
    rating_ok = await service.patch_assistant_message_rating(
        session_id,
        assistant_msg.id,
        user_id=user_id,
        rating=5,
    )

    async with chat_uuid_db.session() as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT CAST(session_id AS TEXT) AS session_id,
                           CAST(user_id AS TEXT) AS user_id
                    FROM messages
                    ORDER BY created_at ASC
                    """
                )
            )
        ).mappings().all()
        count = (
            await session.execute(
                text("SELECT message_count FROM chat_sessions WHERE id = :id"),
                {"id": str(session_id)},
            )
        ).scalar_one()
        status_row = (
            await session.execute(
                text(
                    """
                    SELECT status, error
                    FROM messages
                    WHERE lower(replace(CAST(id AS TEXT), '-', '')) = :mid
                    """
                ),
                {"mid": str(user_msg.id).replace("-", "").lower()},
            )
        ).one()
        rating = (
            await session.execute(
                text(
                    """
                    SELECT rating
                    FROM messages
                    WHERE lower(replace(CAST(id AS TEXT), '-', '')) = :mid
                    """
                ),
                {"mid": str(assistant_msg.id).replace("-", "").lower()},
            )
        ).scalar_one()

    assert [m.content for m in history] == ["hello", "hi"]
    assert filtered == [user_msg.id]
    assert status_ok is True
    assert rating_ok is True
    assert count == 2
    assert [row["session_id"] for row in rows] == [str(session_id), str(session_id)]
    assert [row["user_id"] for row in rows] == [str(user_id), str(user_id)]
    assert status_row._mapping["status"] == MessageStatus.FAILED.value
    assert status_row._mapping["error"] == "boom"
    assert rating == 5


@pytest.mark.asyncio
async def test_tiered_session_store_saves_and_lists_dashed_sqlite_sessions(
    chat_uuid_db: DatabaseService,
) -> None:
    user_id = uuid4()
    session_id = uuid4()
    await _insert_dashed_user_session(
        chat_uuid_db,
        user_id=user_id,
        session_id=session_id,
    )
    store = TieredSessionStore(Settings(), database=chat_uuid_db)
    tool_msg = SessionMessage(
        role="tool",
        content='{"ok": true}',
        tool_call_id="call-1",
    )
    state = SessionState(
        session_id=session_id,
        user_id=user_id,
        messages=[tool_msg],
    )

    await store.save(state)
    loaded = await store.load(session_id)
    listed = await store.list_sessions_for_user(user_id)

    async with chat_uuid_db.session() as session:
        row = (
            await session.execute(
                text(
                    """
                    SELECT CAST(session_id AS TEXT) AS session_id,
                           CAST(user_id AS TEXT) AS user_id
                    FROM messages
                    WHERE role = 'tool'
                    """
                )
            )
        ).mappings().one()
        metadata = (
            await session.execute(
                text("SELECT session_metadata FROM chat_sessions WHERE id = :id"),
                {"id": str(session_id)},
            )
        ).scalar_one()

    assert loaded is not None
    assert loaded.session_id == session_id
    assert [s.session_id for s in listed] == [session_id]
    assert row["session_id"] == str(session_id)
    assert row["user_id"] == str(user_id)
    assert "session_state_v1" in metadata


@pytest.mark.asyncio
async def test_deduplicate_user_messages_repairs_dashed_sqlite_session_rows(
    chat_uuid_db: DatabaseService,
) -> None:
    user_id = uuid4()
    session_id = uuid4()
    await _insert_dashed_user_session(
        chat_uuid_db,
        user_id=user_id,
        session_id=session_id,
        message_count=3,
    )
    now = datetime.utcnow().isoformat(sep=" ")
    async with chat_uuid_db.session() as session:
        for _ in range(3):
            await session.execute(
                text(
                    """
                    INSERT INTO messages (
                        created_at, updated_at, id, role, content, status,
                        session_id, user_id
                    )
                    VALUES (
                        :now, :now, :id, 'user', 'dup', 'completed',
                        :sid, :uid
                    )
                    """
                ),
                {
                    "now": now,
                    "id": str(uuid4()),
                    "sid": str(session_id),
                    "uid": str(user_id),
                },
            )

    async with chat_uuid_db.session() as session:
        result = await deduplicate_user_messages(session, session_id=session_id)

    async with chat_uuid_db.session() as session:
        remaining = (
            await session.execute(
                text("SELECT COUNT(*) FROM messages WHERE session_id = :sid"),
                {"sid": str(session_id)},
            )
        ).scalar_one()
        message_count = (
            await session.execute(
                text("SELECT message_count FROM chat_sessions WHERE id = :sid"),
                {"sid": str(session_id)},
            )
        ).scalar_one()

    assert result["duplicates_found"] == 2
    assert result["rows_deleted"] == 2
    assert remaining == 1
    assert message_count == 1
