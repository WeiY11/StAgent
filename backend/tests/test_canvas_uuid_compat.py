"""SQLite dashed/hex UUID compatibility for canvas persistence paths."""

from __future__ import annotations

import types
from datetime import datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text

from StAgent.api.v1.canvas import list_revisions
from StAgent.config.settings import Settings
from StAgent.db.service import DatabaseService
from StAgent.services.canvas.service import CanvasService, mint_preview_token
from StAgent.services.chat.service import ChatService


@pytest_asyncio.fixture()
async def canvas_uuid_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    db_path = tmp_path / "canvas-uuid.db"
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
) -> None:
    now = datetime.utcnow().isoformat(sep=" ")
    async with db.session() as session:
        await session.execute(
            text("INSERT OR IGNORE INTO users (id, role, disabled) VALUES (:id, 'user', 0)"),
            {"id": str(user_id)},
        )
        await session.execute(
            text(
                """
                INSERT INTO chat_sessions (
                    created_at, updated_at, id, name, user_id, is_active,
                    message_count, last_message_at
                )
                VALUES (:now, :now, :id, 'canvas chat', :uid, 1, 0, :now)
                """
            ),
            {"now": now, "id": str(session_id), "uid": str(user_id)},
        )


async def _insert_dashed_canvas_revision(
    db: DatabaseService,
    *,
    user_id: UUID,
    session_id: UUID,
    canvas_id: UUID,
    revision: int,
    title: str,
    html: str = "<p>ok</p>",
) -> None:
    now = datetime.utcnow().isoformat(sep=" ")
    async with db.session() as session:
        await session.execute(
            text(
                """
                INSERT INTO canvas_documents (
                    created_at, updated_at, id, canvas_id, revision, session_id,
                    user_id, title, content_type, html_body
                )
                VALUES (
                    :now, :now, :id, :canvas_id, :revision, :session_id,
                    :user_id, :title, 'html', :html
                )
                """
            ),
            {
                "now": now,
                "id": str(uuid4()),
                "canvas_id": str(canvas_id),
                "revision": revision,
                "session_id": str(session_id),
                "user_id": str(user_id),
                "title": title,
                "html": html,
            },
        )


def _canvas_service(db: DatabaseService) -> CanvasService:
    settings = Settings()
    settings.canvas.preview_signing_secret = "canvas-uuid-signing-secret" * 2
    settings.canvas.max_html_bytes = 512 * 1024
    settings.canvas.embed_allow_loopback = True
    settings.canvas.max_tree_depth = 8
    settings.canvas.max_nodes_per_tree = 100
    settings.canvas.max_ui_snapshot_bytes = 1024
    chat = ChatService(settings, db_service=db, cache_service=None)
    return CanvasService(settings, db, chat=chat)


@pytest.mark.asyncio
async def test_canvas_service_matches_dashed_sqlite_uuid_rows(
    canvas_uuid_db: DatabaseService,
) -> None:
    user_id = uuid4()
    other_user_id = uuid4()
    session_id = uuid4()
    other_session_id = uuid4()
    canvas_id = uuid4()
    other_canvas_id = uuid4()
    await _insert_dashed_user_session(
        canvas_uuid_db,
        user_id=user_id,
        session_id=session_id,
    )
    await _insert_dashed_user_session(
        canvas_uuid_db,
        user_id=other_user_id,
        session_id=other_session_id,
    )
    await _insert_dashed_canvas_revision(
        canvas_uuid_db,
        user_id=user_id,
        session_id=session_id,
        canvas_id=canvas_id,
        revision=1,
        title="old",
    )
    await _insert_dashed_canvas_revision(
        canvas_uuid_db,
        user_id=user_id,
        session_id=session_id,
        canvas_id=canvas_id,
        revision=2,
        title="latest",
    )
    await _insert_dashed_canvas_revision(
        canvas_uuid_db,
        user_id=other_user_id,
        session_id=other_session_id,
        canvas_id=other_canvas_id,
        revision=1,
        title="foreign",
    )
    service = _canvas_service(canvas_uuid_db)

    listed = await service.list_session_latest_documents(
        user_id=user_id,
        session_id=session_id,
    )
    latest = await service.get_latest_document(user_id=user_id, canvas_id=canvas_id)
    revision = await service.get_revision(
        user_id=user_id,
        canvas_id=canvas_id,
        revision=1,
    )
    preview = await service.fetch_for_preview(
        user_id=user_id,
        canvas_id=canvas_id,
        revision=1,
    )
    token = mint_preview_token(
        service._settings,  # noqa: SLF001 - test verifies token-backed fetch path.
        canvas_id=canvas_id,
        revision=1,
        user_id=user_id,
    )
    token_doc = await service.load_verified_from_token(token)

    assert [(row["canvas_id"], row["revision"], row["title"]) for row in listed] == [
        (str(canvas_id), 2, "latest")
    ]
    assert latest is not None
    assert latest.revision == 2
    assert revision is not None
    assert revision.title == "old"
    assert preview is not None
    assert preview.title == "old"
    assert token_doc is not None
    assert token_doc.title == "old"


@pytest.mark.asyncio
async def test_canvas_revisions_api_matches_dashed_sqlite_uuid_rows(
    canvas_uuid_db: DatabaseService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = uuid4()
    session_id = uuid4()
    canvas_id = uuid4()
    await _insert_dashed_user_session(
        canvas_uuid_db,
        user_id=user_id,
        session_id=session_id,
    )
    for revision, title in [(1, "first"), (2, "second")]:
        await _insert_dashed_canvas_revision(
            canvas_uuid_db,
            user_id=user_id,
            session_id=session_id,
            canvas_id=canvas_id,
            revision=revision,
            title=title,
        )
    monkeypatch.setattr(
        "StAgent.main.get_service_manager",
        lambda: types.SimpleNamespace(db=canvas_uuid_db),
    )

    rows = await list_revisions(canvas_id, user_id, _canvas_service(canvas_uuid_db))

    assert [(row.revision, row.title) for row in rows] == [(1, "first"), (2, "second")]


@pytest.mark.asyncio
async def test_publish_revision_rejects_foreign_existing_canvas_id(
    canvas_uuid_db: DatabaseService,
) -> None:
    user_id = uuid4()
    other_user_id = uuid4()
    session_id = uuid4()
    other_session_id = uuid4()
    canvas_id = uuid4()
    await _insert_dashed_user_session(
        canvas_uuid_db,
        user_id=user_id,
        session_id=session_id,
    )
    await _insert_dashed_user_session(
        canvas_uuid_db,
        user_id=other_user_id,
        session_id=other_session_id,
    )
    await _insert_dashed_canvas_revision(
        canvas_uuid_db,
        user_id=other_user_id,
        session_id=other_session_id,
        canvas_id=canvas_id,
        revision=1,
        title="foreign",
    )
    service = _canvas_service(canvas_uuid_db)

    with pytest.raises(PermissionError, match="canvas not found or denied"):
        await service.publish_revision(
            user_id=user_id,
            session_id=session_id,
            title="attempt",
            mode="html",
            html="<p>attempt</p>",
            canvas_id=canvas_id,
        )


@pytest.mark.asyncio
async def test_publish_revision_continues_owned_dashed_canvas_revision(
    canvas_uuid_db: DatabaseService,
) -> None:
    user_id = uuid4()
    session_id = uuid4()
    canvas_id = uuid4()
    await _insert_dashed_user_session(
        canvas_uuid_db,
        user_id=user_id,
        session_id=session_id,
    )
    await _insert_dashed_canvas_revision(
        canvas_uuid_db,
        user_id=user_id,
        session_id=session_id,
        canvas_id=canvas_id,
        revision=1,
        title="first",
    )
    service = _canvas_service(canvas_uuid_db)

    out = await service.publish_revision(
        user_id=user_id,
        session_id=session_id,
        title="second",
        mode="html",
        html="<p>second</p>",
        canvas_id=canvas_id,
    )

    assert out["revision"] == 2
