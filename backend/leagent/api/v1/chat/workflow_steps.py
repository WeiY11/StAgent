"""Chat workflow step / embed execution endpoints.

Endpoints:

* ``POST /sessions/{id}/workflow-steps/{step_id}/run`` — execute a single
  workflow step tool call after digest verification.
* ``POST /sessions/{id}/workflow-embeds/{message_id}/run`` — execute a whole
  chat workflow DAG embed in the background after digest verification.

Extracted verbatim from the ``chat`` package ``__init__`` aggregator; the
sub-router is mounted there via ``include_router`` so route paths and behavior
are unchanged.
"""

from __future__ import annotations

import json
from contextlib import suppress
from datetime import UTC
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, status

from leagent.api.schemas.chat import (
    ChatWorkflowEmbedRunRequest,
    ChatWorkflowStepRunRequest,
)
from leagent.api.v1.chat._common import _require_session_project_access
from leagent.api.v1.chat.attachments import (
    resolve_request_attachment_paths as _resolve_request_attachment_paths,
)
from leagent.api.v1.chat_deps import ChatSvc
from leagent.db import DatabaseService, get_database_service
from leagent.db.models.message import MessageRole
from leagent.services.auth import CurrentUserId  # noqa: TC001

router = APIRouter()


@router.post("/sessions/{session_id}/workflow-steps/{step_id}/run")
async def run_chat_workflow_step(
    session_id: UUID,
    step_id: str,
    body: ChatWorkflowStepRunRequest,
    user_id: CurrentUserId,
    chat_svc: ChatSvc,
    db: Annotated[DatabaseService, Depends(get_database_service)],
    x_chat_project_token: str | None = Header(default=None, alias="X-Chat-Project-Token"),
) -> dict[str, Any]:
    """Execute a single workflow step tool call after digest verification."""
    from leagent.chat_workflow.arguments import (
        coerce_workflow_step_arguments,
        normalize_workflow_step_arguments,
        validate_workflow_step_paths,
    )
    from leagent.chat_workflow.schema import (
        ValidationError as ChatWorkflowValidationError,
    )
    from leagent.chat_workflow.schema import (
        chat_workflow_digest,
        parse_chat_workflow_spec,
        resolve_argument_templates,
    )
    from leagent.file.attachment_context import tool_extra_for_chat_session
    from leagent.main import get_service_manager
    from leagent.tools.context import build_tool_context
    from leagent.tools.registry import get_registry

    session = await chat_svc.get_session(session_id, user_id=user_id)
    if not session:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
    await _require_session_project_access(
        db,
        session=session,
        user_id=user_id,
        unlock_token=x_chat_project_token,
    )

    msg = await chat_svc.get_session_message(session_id, body.message_id, user_id=user_id)
    if not msg:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Message not found")
    if msg.role != MessageRole.ASSISTANT:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Workflow steps apply only to assistant messages",
        )
    if not msg.extensions:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Message has no workflow data",
        )

    try:
        ext = json.loads(msg.extensions)
    except json.JSONDecodeError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid extensions JSON",
        ) from None

    raw_spec = ext.get("chat_workflow")
    if not isinstance(raw_spec, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No chat_workflow in message extensions",
        )

    registry = get_registry()
    try:
        spec = parse_chat_workflow_spec(raw_spec, registry=registry)
    except ChatWorkflowValidationError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e

    digest_stored = ext.get("chat_workflow_digest")
    if isinstance(digest_stored, str) and len(digest_stored) >= 32:
        digest_ok = digest_stored.lower() == body.workflow_digest.strip().lower()
    else:
        digest_ok = chat_workflow_digest(spec).lower() == body.workflow_digest.strip().lower()
    if not digest_ok:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="workflow_digest does not match stored workflow",
        )

    step = next((s for s in spec.steps if s.id == step_id), None)
    if step is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Step not found")

    sm = None
    with suppress(Exception):
        sm = get_service_manager()

    tool_ctx = build_tool_context(
        service_manager=sm,
        user_id=str(user_id),
        session_id=str(session_id),
    )
    if sm is not None and getattr(sm, "session_manager", None) is not None:
        extra_paths: list[str] | None = None
        raw_ui = (body.user_input or "").strip()
        if raw_ui:
            resolved_refs = await _resolve_request_attachment_paths(
                session_id,
                [raw_ui],
                user_id=user_id,
                reject_unknown=False,
            )
            extra_paths = resolved_refs or None
        att_extra = await tool_extra_for_chat_session(
            sm.session_manager,
            session_id,
            user_id=user_id,
            extra_paths=extra_paths,
        )
        tool_ctx.extra.update(att_extra)

    resolved = resolve_argument_templates(
        step.action.arguments,
        session_id=str(session_id),
        user_id=str(user_id),
        user_input=body.user_input or "",
    )
    resolved = normalize_workflow_step_arguments(step.action.tool_id, resolved, registry=registry)
    resolved = coerce_workflow_step_arguments(step.action.tool_id, resolved, tool_ctx, registry=registry)
    path_error = validate_workflow_step_paths(
        step.action.tool_id, resolved, tool_ctx, registry=registry,
    )
    if path_error:
        runs: dict[str, Any] = ext.get("chat_workflow_step_runs")
        if not isinstance(runs, dict):
            runs = {}
        runs[step_id] = {"status": "error", "error": path_error}
        await chat_svc.merge_message_extensions(
            session_id,
            body.message_id,
            user_id=user_id,
            patch={"chat_workflow_step_runs": runs},
        )
        return {
            "success": False,
            "data": None,
            "error": path_error,
            "duration_ms": 0,
        }

    from leagent.chat_workflow.runner import run_chat_workflow_step_via_engine
    from leagent.runtime.run_store import get_run_store

    parent_run_id = body.parent_run_id
    if not parent_run_id:
        active = get_run_store().get_active_chat_turn(str(session_id))
        if active is not None:
            parent_run_id = active.run_id

    outcome = await run_chat_workflow_step_via_engine(
        spec=spec,
        step_id=step_id,
        resolved_args=resolved,
        tool_ctx=tool_ctx,
        service_manager=sm,
        user_id=str(user_id),
        session_id=str(session_id),
        parent_run_id=parent_run_id,
    )
    result = outcome.tool_result

    from datetime import datetime

    runs: dict[str, Any] = ext.get("chat_workflow_step_runs")
    if not isinstance(runs, dict):
        runs = {}
    completed_at = datetime.now(UTC).isoformat()
    step_entry: dict[str, Any] = {
        "started_at": completed_at,
        "status": "success" if result.success else "error",
        "completed_at": completed_at,
    }
    if outcome.prompt_id:
        step_entry["prompt_id"] = outcome.prompt_id
    if outcome.run_id:
        step_entry["run_id"] = outcome.run_id
    if not result.success and result.error:
        step_entry["error"] = str(result.error)
    runs[step_id] = step_entry
    await chat_svc.merge_message_extensions(
        session_id,
        body.message_id,
        user_id=user_id,
        patch={"chat_workflow_step_runs": runs},
    )

    return {
        "success": result.success,
        "data": result.data,
        "error": result.error,
        "duration_ms": result.duration_ms,
        "prompt_id": outcome.prompt_id,
        "run_id": outcome.run_id,
    }


@router.post("/sessions/{session_id}/workflow-embeds/{message_id}/run")
async def run_chat_workflow_embed(
    session_id: UUID,
    message_id: UUID,
    body: ChatWorkflowEmbedRunRequest,
    user_id: CurrentUserId,
    chat_svc: ChatSvc,
    db: Annotated[DatabaseService, Depends(get_database_service)],
    x_chat_project_token: str | None = Header(default=None, alias="X-Chat-Project-Token"),
) -> dict[str, Any]:
    """Execute a whole chat workflow DAG embed after digest verification.

    Returns ``prompt_id`` so the client can subscribe to the per-node execution
    WebSocket and render live status on the chat mini-graph. The graph runs in
    the background; the terminal status is persisted to message extensions when
    it finishes.
    """
    from datetime import datetime

    from leagent.chat_workflow.runner import (
        evaluate_embed_result,
        start_chat_workflow_embed_via_engine,
    )
    from leagent.main import get_service_manager
    from leagent.runtime.run_store import get_run_store

    if message_id != body.message_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="message_id mismatch between path and body",
        )

    session = await chat_svc.get_session(session_id, user_id=user_id)
    if not session:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
    await _require_session_project_access(
        db,
        session=session,
        user_id=user_id,
        unlock_token=x_chat_project_token,
    )

    msg = await chat_svc.get_session_message(session_id, body.message_id, user_id=user_id)
    if not msg:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Message not found")
    if msg.role != MessageRole.ASSISTANT:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Workflow embeds apply only to assistant messages",
        )
    if not msg.extensions:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Message has no workflow data",
        )

    try:
        ext = json.loads(msg.extensions)
    except json.JSONDecodeError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid extensions JSON",
        ) from None

    embed = ext.get("workflow_embed")
    if not isinstance(embed, dict) or not isinstance(embed.get("data"), dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No workflow_embed in message extensions",
        )

    digest_stored = embed.get("digest") or ext.get("workflow_embed_digest")
    if not isinstance(digest_stored, str) or len(digest_stored) < 16:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Stored workflow embed has no digest",
        )
    if digest_stored.lower() != body.workflow_digest.strip().lower():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="workflow_digest does not match stored workflow",
        )

    sm = None
    with suppress(Exception):
        sm = get_service_manager()

    parent_run_id = body.parent_run_id
    if not parent_run_id:
        active = get_run_store().get_active_chat_turn(str(session_id))
        if active is not None:
            parent_run_id = active.run_id

    msg_id = body.message_id

    async def _persist_terminal(result: Any | None) -> None:
        """Background completion hook: persist terminal status to extensions."""
        from leagent.services.chat.service import get_chat_service

        evaluated = evaluate_embed_result(result)
        entry: dict[str, Any] = {
            "status": "success" if evaluated.success else "error",
            "completed_at": datetime.now(UTC).isoformat(),
        }
        if not evaluated.success and evaluated.error:
            entry["error"] = str(evaluated.error)
        with suppress(Exception):
            svc = get_chat_service()
            await svc.merge_message_extensions(
                session_id,
                msg_id,
                user_id=user_id,
                patch={"workflow_embed_run": entry},
            )

    outcome = await start_chat_workflow_embed_via_engine(
        flow_data=embed["data"],
        service_manager=sm,
        user_id=str(user_id),
        session_id=str(session_id),
        user_input=body.user_input or "",
        inputs=body.inputs,
        parent_run_id=parent_run_id,
        on_complete=_persist_terminal,
    )

    if not outcome.started:
        run_entry = {
            "status": "error",
            "completed_at": datetime.now(UTC).isoformat(),
            "error": outcome.error or "Failed to start workflow",
        }
        await chat_svc.merge_message_extensions(
            session_id,
            body.message_id,
            user_id=user_id,
            patch={"workflow_embed_run": run_entry},
        )
        return {
            "success": False,
            "error": outcome.error,
            "prompt_id": None,
            "run_id": None,
        }

    run_entry = {
        "status": "running",
        "started_at": datetime.now(UTC).isoformat(),
    }
    if outcome.prompt_id:
        run_entry["prompt_id"] = outcome.prompt_id
    if outcome.run_id:
        run_entry["run_id"] = outcome.run_id
    await chat_svc.merge_message_extensions(
        session_id,
        body.message_id,
        user_id=user_id,
        patch={"workflow_embed_run": run_entry},
    )

    return {
        "success": True,
        "status": "running",
        "prompt_id": outcome.prompt_id,
        "run_id": outcome.run_id,
    }
