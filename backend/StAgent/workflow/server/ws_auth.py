"""Shared WebSocket authentication for the workflow routers.

Lifted out of :mod:`router` so every workflow WebSocket endpoint — the existing
per-prompt and monitor streams plus the observability stream — validates
credentials through exactly one implementation.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import WebSocket

__all__ = ["authenticate_websocket"]


async def authenticate_websocket(
    websocket: WebSocket,
    *,
    require_admin: bool = False,
) -> tuple[UUID, str | None] | None:
    """Validate WS credentials.

    Returns ``(user_id, accept_subprotocol)`` or closes the socket and returns
    ``None``. Credentials are read from the ``Authorization`` header, then the
    ``Sec-WebSocket-Protocol`` handshake (``bearer,<jwt>`` — browsers cannot set
    headers on a WebSocket), then a ``?token=`` query parameter. When auth is not
    enforced the local single-user identity is assumed.
    """
    from StAgent.config.constants import LOCAL_USER_ID
    from StAgent.config.settings import get_settings
    from StAgent.services.auth.policy import effective_enforce_auth
    from StAgent.services.auth.service import get_auth_service

    token: str | None = None
    accept_subprotocol: str | None = None
    auth = websocket.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip() or None
    if not token:
        proto = websocket.headers.get("sec-websocket-protocol") or ""
        parts = [p.strip() for p in proto.split(",") if p.strip()]
        if len(parts) >= 2 and parts[0].lower() == "bearer":
            token = parts[1]
            accept_subprotocol = "bearer"
        elif len(parts) == 1 and parts[0].lower() != "bearer":
            # Some clients send the raw JWT as the sole subprotocol.
            token = parts[0]
            accept_subprotocol = parts[0]
    if not token:
        token = websocket.query_params.get("token")

    settings = get_settings()
    enforced = effective_enforce_auth(settings)
    if not token:
        if enforced:
            await websocket.close(code=4001, reason="Authentication required")
            return None
        return LOCAL_USER_ID, None

    svc = get_auth_service()
    payload = svc.decode_access_token(token)
    if payload is None:
        await websocket.close(code=4001, reason="Invalid token")
        return None
    uid = UUID(payload.sub)
    if require_admin and payload.role != "admin" and uid != LOCAL_USER_ID:
        await websocket.close(code=4003, reason="Admin required")
        return None
    return uid, accept_subprotocol
