"""WebSocket connection-manager compatibility contracts."""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from StAgent.api.v1.chat import websocket as chat_websocket
from StAgent.api.v1.chat.websocket import ConnectionManager

if TYPE_CHECKING:
    from fastapi import WebSocket


class _FakeWebSocket:
    def __init__(self) -> None:
        self.accepted = False
        self.sent: list[dict[str, Any]] = []

    async def accept(self) -> None:
        self.accepted = True

    async def send_json(self, payload: dict[str, Any]) -> None:
        self.sent.append(payload)


class _EndpointWebSocket(_FakeWebSocket):
    """Minimal WebSocket contract for one authenticated chat message."""

    def __init__(self) -> None:
        super().__init__()
        self.headers = {"authorization": "Bearer test-token"}
        self.query_params: dict[str, str] = {}

    async def receive_json(self) -> dict[str, str]:
        return {"type": "message", "content": "@knowledge:remote-guide"}


@pytest.mark.asyncio
async def test_connection_manager_supports_personal_messages() -> None:
    manager = ConnectionManager()
    fake = _FakeWebSocket()
    websocket = cast("WebSocket", fake)

    await manager.connect("session-1", websocket)
    await manager.send_personal_message({"type": "pong"}, websocket)

    assert fake.accepted is True
    assert fake.sent == [{"type": "pong"}]
    assert manager.active_connections == {"session-1": [websocket]}

    manager.disconnect("session-1", websocket)
    assert manager.active_connections == {}


@pytest.mark.asyncio
async def test_websocket_releases_knowledge_lease_when_agent_stream_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A WebSocket agent-stream failure must still tear down staged knowledge."""
    user_id = uuid4()
    session_id = uuid4()
    lease_state = {"entered": False, "exited": False}
    turn_manager = ConnectionManager()

    @asynccontextmanager
    async def _knowledge_lease(*_args: object, **_kwargs: object):
        lease_state["entered"] = True
        try:
            yield ["C:/temporary/knowledge-source.txt"]
        finally:
            lease_state["exited"] = True

    async def _allow_project_access(*_args: object, **_kwargs: object) -> None:
        return None

    async def _broken_agent_stream(*_args: object, **_kwargs: object):
        yield "token", {"token": "partial"}, "partial"
        raise RuntimeError("agent stream exploded")

    monkeypatch.setattr(chat_websocket, "manager", turn_manager)
    monkeypatch.setattr(
        "StAgent.services.auth.get_auth_service",
        lambda: SimpleNamespace(verify_access_token=lambda _token: user_id),
    )
    monkeypatch.setattr(
        chat_websocket,
        "_require_session_project_access",
        _allow_project_access,
    )
    monkeypatch.setattr(chat_websocket, "build_agent_controller", lambda: object())
    monkeypatch.setattr(
        "StAgent.services.chat.pet_personality.apply_pet_personality_to_agent",
        AsyncMock(),
    )
    monkeypatch.setattr(
        chat_websocket,
        "_authorized_root_paths_for_session",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        chat_websocket,
        "_materialize_knowledge_message_paths",
        _knowledge_lease,
    )
    monkeypatch.setattr(chat_websocket, "_run_agent_stream", _broken_agent_stream)

    chat_service = SimpleNamespace(
        get_session=AsyncMock(return_value=SimpleNamespace(id=session_id)),
        add_message=AsyncMock(return_value=SimpleNamespace(id=uuid4())),
    )
    fake = _EndpointWebSocket()

    await chat_websocket.websocket_endpoint(
        cast("WebSocket", fake),
        session_id,
        chat_service,
        SimpleNamespace(),
    )

    assert lease_state == {"entered": True, "exited": True}
    assert turn_manager.active_connections == {}
