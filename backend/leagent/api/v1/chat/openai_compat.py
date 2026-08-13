"""OpenAI-compatible chat completions endpoint.

Split out of ``chat/__init__.py`` to keep the package cohesive: this is a
self-contained alternate API surface (``POST /completions``) that mirrors the
OpenAI Chat Completions contract (streaming SSE + non-streaming), reusing the
same agent think-act loop as the native ``/stream`` endpoint.

Helpers are aliased to their historical leading-underscore names on import so
the route/generator bodies remain identical to their previous in-package form.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator  # noqa: TC003
from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID, uuid4

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, status
from sse_starlette.sse import EventSourceResponse

from leagent.api.schemas.chat import (
    ChatCompletionChoice,
    ChatCompletionMessage,
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatCompletionUsage,
)
from leagent.api.v1.chat._common import (
    _accumulate_workflow_extensions,
    _require_project_access,
    _require_session_project_access,
)
from leagent.api.v1.chat.agent_stream import run_agent_stream as _run_agent_stream
from leagent.api.v1.chat.attachments import (
    merge_agent_attachment_paths as _merge_agent_attachment_paths,
)
from leagent.api.v1.chat.context_sources import (
    authorized_root_paths_for_session as _authorized_root_paths_for_session,
)
from leagent.api.v1.chat.context_sources import (
    materialize_knowledge_message_paths as _materialize_knowledge_message_paths,
)
from leagent.api.v1.chat.sse import format_openai_chunk as _format_openai_chunk
from leagent.api.v1.chat_deps import ChatSvc, build_agent_controller
from leagent.db import DatabaseService, get_database_service
from leagent.db.models.message import MessageRole
from leagent.services.auth import CurrentUserId  # noqa: TC001

logger = structlog.get_logger(__name__)

router = APIRouter()


def _requested_agent_variant(raw: str | None) -> str | None:
    """Return a registered agent prompt variant, ignoring unknown client input."""
    candidate = (raw or "").strip()
    if not candidate:
        return None
    from leagent.context.recipe import RECIPE_REGISTRY

    return candidate if candidate in RECIPE_REGISTRY else None


def _agent_model_name(model: str | None) -> str | None:
    """Map the OpenAI-compatible model field onto AgentConfig.model_name."""
    candidate = (model or "").strip()
    return candidate if candidate and candidate != "default" else None


async def _generate_openai_sse(
    request: ChatCompletionRequest,
    session_id: UUID,
    user_id: UUID,
    chat_svc: ChatSvc,
    *,
    attachments: list[str] | None = None,
    persisted_user_message_id: UUID | None = None,
    db: DatabaseService | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Generate OpenAI-compatible SSE chunks."""

    completion_id = f"chatcmpl-{uuid4().hex[:24]}"
    created = int(time.time())
    model = request.model
    start_time = time.time()
    output_tokens = 0

    try:
        yield _format_openai_chunk(completion_id, created, model, {"role": "assistant", "content": ""})

        response_content = ""
        last_extensions_json: str | None = None
        agent = build_agent_controller(
            prompt_variant=_requested_agent_variant(request.agent_variant),
            extra_system_prompt=request.system_prompt,
            model_name=_agent_model_name(request.model),
            temperature=request.temperature,
        )

        if agent is not None:
            from leagent.services.chat.pet_personality import apply_pet_personality_to_agent

            try:
                await apply_pet_personality_to_agent(agent, get_database_service(), user_id)
            except Exception:
                logger.debug("openai_sse_apply_pet_personality_failed", exc_info=True)
            last_user_msg = next(
                (m.content for m in reversed(request.messages) if m.role == MessageRole.USER),
                "",
            )
            openai_auth_roots = await _authorized_root_paths_for_session(
                chat_svc,
                session_id,
                user_id,
            )
            openai_agent_task_id = uuid4()
            async with _materialize_knowledge_message_paths(
                user_id,
                db,
                last_user_msg,
            ) as knowledge_paths:
                merged_attachments = _merge_agent_attachment_paths(
                    attachments,
                    knowledge_paths,
                )
                async for etype, edata, acc_text in _run_agent_stream(
                    agent,
                    last_user_msg,
                    session_id,
                    user_id,
                    attachments=merged_attachments,
                    authorized_roots=openai_auth_roots,
                    persisted_user_message_id=persisted_user_message_id,
                    agent_task_id=openai_agent_task_id,
                ):
                    response_content = acc_text
                    if etype == "token":
                        token = edata.get("token", "")
                        output_tokens += 1
                        yield _format_openai_chunk(completion_id, created, model, {"content": token})
                    elif etype == "thinking":
                        yield {"event": "thinking", "data": json.dumps({"thought": edata.get("thought", "")})}
                    elif etype == "tool_call_delta":
                        yield {"event": "tool_call_delta", "data": json.dumps(edata)}
                    elif etype == "nested_agent_preview":
                        yield {"event": "nested_agent_preview", "data": json.dumps(edata)}
                    elif etype in ("tool_call", "tool_result"):
                        yield {"event": etype, "data": json.dumps(edata)}
                    elif etype == "workflow":
                        yield {"event": "workflow", "data": json.dumps(edata)}
                        if isinstance(edata, dict):
                            last_extensions_json = _accumulate_workflow_extensions(last_extensions_json, edata)
                    elif etype == "complete" and response_content and output_tokens == 0:
                        yield _format_openai_chunk(completion_id, created, model, {"content": response_content})
                    elif etype == "error":
                        yield {
                            "event": "error",
                            "data": json.dumps({"error": edata.get("error", "Unknown error"), "type": "agent_error"}),
                        }
        else:
            yield {
                "event": "error",
                "data": json.dumps(
                    {
                        "error": "No LLM provider configured",
                        "type": "configuration_error",
                    }
                ),
            }

        yield _format_openai_chunk(completion_id, created, model, {}, finish_reason="stop")
        yield {"event": "message", "data": "[DONE]"}

        latency_ms = int((time.time() - start_time) * 1000)
        if response_content or last_extensions_json:
            await chat_svc.add_message(
                session_id,
                MessageRole.ASSISTANT,
                response_content or "",
                user_id=user_id,
                model=model,
                output_tokens=output_tokens or None,
                latency_ms=latency_ms,
                extensions=last_extensions_json,
            )

    except Exception as e:
        logger.exception("Error in SSE stream: %s", e)
        yield {"event": "error", "data": json.dumps({"error": str(e), "type": "stream_error"})}


@router.post("/completions", response_model=None)
async def create_chat_completion(
    request: ChatCompletionRequest,
    user_id: CurrentUserId,
    chat_svc: ChatSvc,
    db: Annotated[DatabaseService, Depends(get_database_service)],
    x_chat_project_token: str | None = Header(default=None, alias="X-Chat-Project-Token"),
) -> EventSourceResponse | ChatCompletionResponse:
    """Create a chat completion (OpenAI-compatible). Streaming or non-streaming."""
    if not request.messages:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Messages list cannot be empty",
        )

    session_id = request.session_id
    if not session_id:
        await _require_project_access(
            db,
            project_id=request.project_id,
            user_id=user_id,
            unlock_token=x_chat_project_token,
        )
        new_session = await chat_svc.create_session(
            user_id,
            name=f"Chat {datetime.now(UTC).strftime('%Y-%m-%d %H:%M')}",
            project_id=request.project_id,
        )
        session_id = new_session.id
    else:
        existing_session = await chat_svc.get_session(session_id, user_id=user_id)
        if not existing_session:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
        await _require_session_project_access(
            db,
            session=existing_session,
            user_id=user_id,
            unlock_token=x_chat_project_token,
        )

    last_user_message = next(
        (m for m in reversed(request.messages) if m.role == MessageRole.USER),
        None,
    )
    persisted_openai_user_id: UUID | None = None
    if last_user_message:
        um_row = await chat_svc.add_message(
            session_id,
            MessageRole.USER,
            last_user_message.content,
            user_id=user_id,
        )
        persisted_openai_user_id = um_row.id

    last_user_text = last_user_message.content if last_user_message else ""

    if request.stream:
        return EventSourceResponse(
            _generate_openai_sse(
                request,
                session_id,
                user_id,
                chat_svc,
                persisted_user_message_id=persisted_openai_user_id,
                db=db,
            ),
            media_type="text/event-stream",
        )

    completion_id = f"chatcmpl-{uuid4().hex[:24]}"
    created = int(time.time())

    # Per-app agent customization (leagent.js custom chatbots / agent apps).
    # Unknown variants are ignored by the context layer (default recipe used).
    agent = build_agent_controller(
        prompt_variant=_requested_agent_variant(request.agent_variant),
        extra_system_prompt=request.system_prompt,
        model_name=_agent_model_name(request.model),
        temperature=request.temperature,
    )
    if agent is not None:
        from leagent.services.chat.pet_personality import apply_pet_personality_to_agent

        await apply_pet_personality_to_agent(agent, db, user_id)
        last_user_msg = next(
            (m.content for m in reversed(request.messages) if m.role == MessageRole.USER),
            "",
        )
        completion_auth_roots = await _authorized_root_paths_for_session(
            chat_svc,
            session_id,
            user_id,
        )
        async with _materialize_knowledge_message_paths(
            user_id,
            db,
            last_user_text,
        ) as knowledge_paths:
            agent_response = await agent.run(
                last_user_msg,
                session_id,
                user_id=user_id,
                attachments=_merge_agent_attachment_paths(None, knowledge_paths),
                authorized_roots=completion_auth_roots,
                persisted_user_message_id=persisted_openai_user_id,
                agent_task_id=uuid4(),
            )
            response_content = agent_response.text
    else:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No LLM provider configured. Please configure a model in Settings.",
        )

    await chat_svc.add_message(
        session_id,
        MessageRole.ASSISTANT,
        response_content,
        user_id=user_id,
        model=request.model,
        output_tokens=len(response_content.split()),
    )

    return ChatCompletionResponse(
        id=completion_id,
        created=created,
        model=request.model,
        session_id=session_id,
        choices=[
            ChatCompletionChoice(
                index=0,
                message=ChatCompletionMessage(role=MessageRole.ASSISTANT, content=response_content),
                finish_reason="stop",
            )
        ],
        usage=ChatCompletionUsage(
            prompt_tokens=sum(len(m.content.split()) for m in request.messages),
            completion_tokens=len(response_content.split()),
            total_tokens=sum(len(m.content.split()) for m in request.messages) + len(response_content.split()),
        ),
    )
