"""Contract tests for ``POST /sessions/{session_id}/fork``.

Messages are seeded straight into the app database (sync ``sqlite3``, same
trick as ``test_missions_api``) so the fork only exercises the copy path —
no agent/LLM involvement. ``users.id``/``messages.id`` are CHAR(32) UUID hex.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from leagent.config.settings import get_settings
from leagent.services.auth.service import LOCAL_USER_ID

if TYPE_CHECKING:
    from fastapi.testclient import TestClient


def _db_path() -> str:
    return str(get_settings().database._sqlite_file_path())


def _insert_message(
    session_id: str,
    *,
    role: str,
    content: str,
    created_at: datetime,
    model: str | None = None,
    tool_calls: str | None = None,
    extensions: str | None = None,
) -> str:
    message_id = uuid.uuid4().hex
    with sqlite3.connect(_db_path()) as conn:
        conn.execute(
            """
            INSERT INTO messages (
                id, created_at, updated_at, session_id, user_id, role,
                content, status, model, tool_calls, extensions
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'completed', ?, ?, ?)
            """,
            (
                message_id,
                created_at.strftime("%Y-%m-%d %H:%M:%S.%f"),
                created_at.strftime("%Y-%m-%d %H:%M:%S.%f"),
                session_id,
                LOCAL_USER_ID.hex if role == "user" else None,
                role,
                content,
                model,
                tool_calls,
                extensions,
            ),
        )
    # Return the dashed UUID form for the REST body.
    return str(uuid.UUID(hex=message_id))


@pytest.fixture()
def seeded_session(client: TestClient) -> dict[str, object]:
    created = client.post("/api/v1/chat/sessions", json={"name": "origin"})
    assert created.status_code == 201
    session_id: str = created.json()["id"]

    base = datetime.utcnow() - timedelta(minutes=10)
    ids = {
        "user1": _insert_message(session_id, role="user", content="hello", created_at=base),
        "assistant1": _insert_message(
            session_id,
            role="assistant",
            content="hi there",
            created_at=base + timedelta(seconds=1),
            model="test-model",
            tool_calls='[{"id": "call_1", "name": "web_search"}]',
        ),
        "user2": _insert_message(
            session_id, role="user", content="second question", created_at=base + timedelta(seconds=2)
        ),
        "assistant2": _insert_message(
            session_id, role="assistant", content="second answer", created_at=base + timedelta(seconds=3)
        ),
    }
    # A system message that must be skipped by the fork.
    _insert_message(session_id, role="system", content="system prompt", created_at=base + timedelta(seconds=4))

    try:
        yield {"session_id": session_id, "message_ids": ids}
    finally:
        with sqlite3.connect(_db_path()) as conn:
            conn.execute(
                "DELETE FROM messages WHERE session_id = ?", (session_id,)
            )


def _list_messages(client: TestClient, session_id: str) -> list[dict]:
    resp = client.get(f"/api/v1/chat/sessions/{session_id}/messages")
    assert resp.status_code == 200
    return resp.json()["items"]


def test_fork_copies_message_prefix(client: TestClient, seeded_session) -> None:
    session_id = seeded_session["session_id"]
    ids: dict[str, str] = seeded_session["message_ids"]  # type: ignore[assignment]

    resp = client.post(
        f"/api/v1/chat/sessions/{session_id}/fork",
        json={"up_to_message_id": ids["assistant1"], "new_name": "my fork"},
    )
    assert resp.status_code == 201
    fork_id = resp.json()["session_id"]

    # Forked session: visible to the owner, inherits the name override.
    detail = client.get(f"/api/v1/chat/sessions/{fork_id}").json()
    assert detail["name"] == "my fork"
    assert detail["message_count"] == 2

    # Prefix copy: user1 + assistant1 only; system rows skipped; content preserved.
    copied = _list_messages(client, fork_id)
    assert [m["role"] for m in copied] == ["user", "assistant"]
    assert [m["content"] for m in copied] == ["hello", "hi there"]
    assert copied[1]["model"] == "test-model"
    assert copied[1]["tool_calls"] == [{"id": "call_1", "name": "web_search"}]

    # New message IDs — a copy, not a reference.
    assert str(copied[0]["id"]) != ids["user1"]

    # Source session untouched.
    assert len(_list_messages(client, session_id)) >= 4

    client.delete(f"/api/v1/chat/sessions/{fork_id}")


def test_fork_defaults_name_and_inherits_project(client: TestClient, seeded_session) -> None:
    session_id = seeded_session["session_id"]
    ids: dict[str, str] = seeded_session["message_ids"]  # type: ignore[assignment]

    resp = client.post(
        f"/api/v1/chat/sessions/{session_id}/fork",
        json={"up_to_message_id": ids["user2"]},
    )
    assert resp.status_code == 201
    fork_id = resp.json()["session_id"]
    detail = client.get(f"/api/v1/chat/sessions/{fork_id}").json()
    assert detail["name"] == "Fork: origin"
    assert detail["message_count"] == 3

    client.delete(f"/api/v1/chat/sessions/{fork_id}")


def test_fork_unknown_message_returns_404(client: TestClient, seeded_session) -> None:
    session_id = seeded_session["session_id"]
    resp = client.post(
        f"/api/v1/chat/sessions/{session_id}/fork",
        json={"up_to_message_id": str(uuid.uuid4())},
    )
    assert resp.status_code == 404


def test_fork_unknown_session_returns_404(client: TestClient) -> None:
    resp = client.post(
        f"/api/v1/chat/sessions/{uuid.uuid4()}/fork",
        json={"up_to_message_id": str(uuid.uuid4())},
    )
    assert resp.status_code == 404
