"""Real-time chat WebSocket endpoint + connection manager.

The ``manager`` singleton is re-exported from ``StAgent.api.v1.chat`` for
backward compatibility (``StAgent.main`` imports it as ``chat_ws_manager``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any
from uuid import UUID, uuid4

import structlog
from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    WebSocket,
    WebSocketDisconnect,
)

from StAgent.api.v1.chat._common import _require_session_project_access
from StAgent.api.v1.chat.agent_stream import run_agent_stream as _run_agent_stream
from StAgent.api.v1.chat.attachments import (
    merge_agent_attachment_paths as _merge_agent_attachment_paths,
)
from StAgent.api.v1.chat.context_sources import (
    authorized_root_paths_for_session as _authorized_root_paths_for_session,
)
from StAgent.api.v1.chat.context_sources import (
    materialize_knowledge_message_paths as _materialize_knowledge_message_paths,
)
from StAgent.api.v1.chat_deps import ChatSvc, build_agent_controller
from StAgent.apps.gateway.infrastructure.ws_fanout import (
    DistributedConnectionManager,
)
from StAgent.db import DatabaseService, get_database_service
from StAgent.db.models.message import MessageRole

logger = structlog.get_logger(__name__)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

router = APIRouter()


class ConnectionManager(DistributedConnectionManager):
    @property
    def active_connections(self) -> dict[str, list[WebSocket]]:
        return self._local

    async def send_personal_message(
        self,
        message: dict[str, Any],
        websocket: WebSocket,
    ) -> None:
        """Send a structured event to one connected client."""
        await websocket.send_json(message)


manager = ConnectionManager()


def _resolve_ws_token(websocket: WebSocket) -> str | None:
    """Resolve the WS auth token.

    Prefers header-based transports that don't leak into access/proxy logs:
    ``Authorization: Bearer`` then ``Sec-WebSocket-Protocol`` (``bearer,<token>``),
    falling back to the legacy ``?token=`` query parameter for compatibility.
    """
    auth = websocket.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        tok = auth[7:].strip()
        if tok:
            return tok
    proto = websocket.headers.get("sec-websocket-protocol") or ""
    if proto:
        parts = [p.strip() for p in proto.split(",") if p.strip()]
        if len(parts) >= 2 and parts[0].lower() in {"bearer", "authorization"}:
            return parts[1]
    return websocket.query_params.get("token")


@router.websocket("/ws/{session_id}")
async def websocket_endpoint(
    websocket: WebSocket,
    session_id: UUID,
    chat_svc: ChatSvc,
    db: Annotated[DatabaseService, Depends(get_database_service)],
) -> None:
    """WebSocket endpoint for real-time chat."""
    token_str = _resolve_ws_token(websocket)
    if not token_str:
        await websocket.close(code=4001, reason="Authentication required")
        return

    from StAgent.services.auth import get_auth_service

    auth_service = get_auth_service()
    user_id = auth_service.verify_access_token(token_str)
    if not user_id:
        await websocket.close(code=4001, reason="Invalid token")
        return

    session = await chat_svc.get_session(session_id, user_id=user_id)
    if not session:
        await websocket.close(code=4003, reason="Session not found or access denied")
        return
    project_token = websocket.headers.get("x-chat-project-token") or websocket.query_params.get("project_token")
    try:
        await _require_session_project_access(
            db,
            session=session,
            user_id=user_id,
            unlock_token=project_token,
        )
    except HTTPException as exc:
        await websocket.close(code=4003, reason=str(exc.detail))
        return

    session_key = str(session_id)
    await manager.connect(session_key, websocket)

    try:
        while True:
            data = await websocket.receive_json()
            msg_type = data.get("type", "message")

            if msg_type == "ping":
                await manager.send_personal_message({"type": "pong"}, websocket)
                continue

            if msg_type == "message":
                content = data.get("content", "")
                model = data.get("model", "default")

                if not content:
                    await manager.send_personal_message(
                        {"type": "error", "error": "Empty message content"},
                        websocket,
                    )
                    continue

                user_message = await chat_svc.add_message(
                    session_id,
                    MessageRole.USER,
                    content,
                    user_id=user_id,
                )

                await manager.send_personal_message(
                    {"type": "message_received", "message_id": str(user_message.id)},
                    websocket,
                )

                response_content = ""
                agent = build_agent_controller()

                if agent is not None:
                    from StAgent.services.chat.pet_personality import apply_pet_personality_to_agent

                    await apply_pet_personality_to_agent(agent, db, user_id)
                    ws_auth_roots = await _authorized_root_paths_for_session(
                        chat_svc,
                        session_id,
                        user_id,
                    )
                    ws_agent_task_id = uuid4()
                    await manager.send_personal_message(
                        {
                            "type": "agent_task",
                            "task_id": str(ws_agent_task_id),
                            "session_id": str(session_id),
                        },
                        websocket,
                    )

                    async def _stream_with_knowledge_paths(
                        *,
                        turn_agent: Any = agent,
                        turn_content: str = content,
                        turn_authorized_roots: list[str] | None = ws_auth_roots,
                        turn_message_id: UUID = user_message.id,
                        turn_task_id: UUID = ws_agent_task_id,
                    ) -> AsyncIterator[tuple[str, dict[str, Any], str]]:
                        async with _materialize_knowledge_message_paths(
                            user_id,
                            db,
                            turn_content,
                        ) as knowledge_paths:
                            ws_attachments = _merge_agent_attachment_paths(
                                None,
                                knowledge_paths,
                            )
                            async for event in _run_agent_stream(
                                turn_agent,
                                turn_content,
                                session_id,
                                user_id,
                                attachments=ws_attachments,
                                authorized_roots=turn_authorized_roots,
                                persisted_user_message_id=turn_message_id,
                                agent_task_id=turn_task_id,
                            ):
                                yield event

                    async for etype, edata, acc_text in _stream_with_knowledge_paths():
                        response_content = acc_text
                        if etype == "token":
                            await manager.send_personal_message(
                                {"type": "stream", "content": edata.get("token", "")},
                                websocket,
                            )
                        elif etype in (
                            "thinking",
                            "tool_call",
                            "tool_call_delta",
                            "tool_result",
                            "nested_agent_preview",
                        ):
                            await manager.send_personal_message(
                                {"type": etype, **edata},
                                websocket,
                            )
                        elif etype == "error":
                            await manager.send_personal_message(
                                {"type": "error", "error": edata.get("error", "")},
                                websocket,
                            )
                else:
                    await manager.send_personal_message(
                        {"type": "error", "error": "No LLM provider configured"},
                        websocket,
                    )

                if response_content:
                    assistant_message = await chat_svc.add_message(
                        session_id,
                        MessageRole.ASSISTANT,
                        response_content,
                        user_id=user_id,
                        model=model,
                        output_tokens=len(response_content.split()),
                    )
                    await manager.send_personal_message(
                        {
                            "type": "complete",
                            "message_id": str(assistant_message.id),
                            "content": response_content,
                        },
                        websocket,
                    )

    except WebSocketDisconnect:
        manager.disconnect(session_key, websocket)
    except Exception as e:
        logger.exception("WebSocket error for session %s: %s", session_id, e)
        manager.disconnect(session_key, websocket)
