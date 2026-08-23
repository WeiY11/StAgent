"""Session-scoped agent todo endpoints.

Endpoints:

* ``PATCH /sessions/{id}/todos/{todo_id}`` — update one session-scoped agent
  todo status (manual UI interaction).
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, status

from StAgent.api.schemas.chat import SessionTodoStatusPatchRequest
from StAgent.api.v1.chat._common import _require_session_project_access
from StAgent.api.v1.chat_deps import ChatSvc
from StAgent.db import DatabaseService, get_database_service
from StAgent.db.models.message import SessionRead, chat_session_to_read
from StAgent.services.auth import CurrentUserId  # noqa: TC001

logger = structlog.get_logger(__name__)

router = APIRouter()


@router.patch(
    "/sessions/{session_id}/todos/{todo_id}",
    response_model=SessionRead,
)
async def patch_session_todo_status(
    session_id: UUID,
    todo_id: str,
    body: SessionTodoStatusPatchRequest,
    user_id: CurrentUserId,
    chat_svc: ChatSvc,
    db: Annotated[DatabaseService, Depends(get_database_service)],
    x_chat_project_token: str | None = Header(default=None, alias="X-Chat-Project-Token"),
) -> SessionRead:
    """Update one session-scoped agent todo status (manual UI interaction)."""
    existing = await chat_svc.get_session(session_id, user_id=user_id)
    if not existing:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
    await _require_session_project_access(
        db,
        session=existing,
        user_id=user_id,
        unlock_token=x_chat_project_token,
    )

    from StAgent.main import get_service_manager

    try:
        sm = get_service_manager()
    except Exception:  # noqa: BLE001
        sm = None

    if sm is None or sm.session_manager is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Session manager unavailable",
        )

    try:
        await sm.session_manager.update_todo_status(
            session_id,
            todo_id,
            body.status,
            user_id=user_id,
        )
    except PermissionError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Session not found",
        ) from exc
    except ValueError as exc:
        detail = str(exc)
        if "not found" in detail.lower():
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail) from exc
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail) from exc

    final = await chat_svc.get_session(session_id, user_id=user_id)
    if not final:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
    return chat_session_to_read(final)
