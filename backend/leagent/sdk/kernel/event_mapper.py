"""Kernel event mapper — translates AgentEvent → StreamHandler calls.

This module extracts the event-to-stream mapping logic from
AgentController._run_via_query_engine into a standalone class.
The mapper drives the StreamHandler (SSE) and mutates the
ConversationContext based on kernel events, while the controller
retains ownership of engine setup, error handling, and final
response assembly.

The mapper is the "compat shim" layer: it converts the kernel's
AgentEvent taxonomy into the legacy StreamEvent shape that the
SSE wire contract expects.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from leagent.agent.base import (
    ConversationContext,
    ExecutionStep,
    StepType,
    StreamHandler,
    ToolCall,
    ToolResult,
)
from leagent.agent.transitions import TerminalReason
from leagent.utils.logging import get_logger

if TYPE_CHECKING:
    from leagent.agent.base import AgentContext
    from leagent.sdk.events import AgentEvent

logger = get_logger(__name__)


class KernelEventMapper:
    """Translate kernel AgentEvents into StreamHandler calls + conversation mutations.

    The mapper accumulates state across events (response_text, reasoning_acc)
    and exposes a ``process_event`` async method that the controller drives
    inside the ``async for event in run_loop(...)`` loop.

    When a ``result`` event is processed, ``process_event`` returns an
    ``AgentResponse`` (the final turn result). The controller checks for
    a non-None return to break out of the loop.
    """

    def __init__(
        self,
        *,
        handler: StreamHandler,
        conversation: ConversationContext,
        context: AgentContext,
        update_task_phase: Any,  # callable(session_id, task_id, phase, tool_name=None)
        stash_checkpoint: Any,  # callable(session_id, checkpoint_id)
    ) -> None:
        self.handler = handler
        self.conversation = conversation
        self.context = context
        self._update_task_phase = update_task_phase
        self._stash_checkpoint = stash_checkpoint

        # Accumulated state
        self.response_text = ""
        self.reasoning_acc = ""
        self.streaming_phase_marked = False
        self.last_checkpoint_id: str | None = None

    async def process_event(
        self,
        event: AgentEvent,
        *,
        turn_message_start: int,
    ) -> Any:
        """Process one kernel event. Returns an AgentResponse on ``result``, else None."""
        mtype = event.type
        data = event.data

        if mtype == "stream_delta":
            await self._handle_stream_delta(data)
        elif mtype == "tool_call_delta":
            await self._handle_tool_call_delta(data)
        elif mtype == "tool_use":
            await self._handle_tool_use(data)
        elif mtype == "tool_result":
            await self._handle_tool_result(data)
        elif mtype == "workspace_attachments":
            await self._handle_workspace_attachments(data)
        elif mtype == "assistant_tools":
            self._handle_assistant_tools(data)
        elif mtype == "assistant":
            self._handle_assistant(data)
        elif mtype == "steer":
            self._handle_steer(data)
        elif mtype == "result":
            return await self._handle_result(data, turn_message_start=turn_message_start)
        return None

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    async def _handle_stream_delta(self, data: dict[str, Any]) -> None:
        if not self.streaming_phase_marked:
            self._update_task_phase(
                self.context.session_id,
                self.context.task_id,
                "streaming",
            )
            self.streaming_phase_marked = True
        token = data.get("content", "")
        if isinstance(token, str) and token:
            self.response_text += token
            await self.handler.on_token(token)
        rd = data.get("reasoning_delta")
        if isinstance(rd, str) and rd:
            self.reasoning_acc += rd
            await self.handler.on_thinking(self.reasoning_acc)

    async def _handle_tool_call_delta(self, data: Any) -> None:
        if isinstance(data, dict):
            await self.handler.on_tool_call_delta(data)

    async def _handle_tool_use(self, data: dict[str, Any]) -> None:
        tc = ToolCall(
            id=data.get("id", ""),
            name=data.get("name", ""),
            arguments=data.get("input") or {},
        )
        self._update_task_phase(
            self.context.session_id,
            self.context.task_id,
            "tool",
            tool_name=tc.name or None,
        )
        await self.handler.on_tool_call(tc)
        self.context.record_step(ExecutionStep(type=StepType.TOOL_CALL, tool_call=tc))

    async def _handle_tool_result(self, data: Any) -> None:
        res = _tool_result_from_query_sdk(data if isinstance(data, dict) else {})
        await self.handler.on_tool_result(res)
        self.context.record_step(ExecutionStep(type=StepType.TOOL_RESULT, tool_result=res))
        self.conversation.append_tool_result(res.tool_call_id, res.name, res.content)

    async def _handle_workspace_attachments(self, data: Any) -> None:
        if not isinstance(data, dict):
            return
        attachments_payload = data.get("attachments")
        if isinstance(attachments_payload, list):
            ingest = getattr(self.handler, "on_workspace_attachments", None)
            if ingest is not None:
                await ingest([item for item in attachments_payload if isinstance(item, dict)])
        paths_payload = data.get("paths")
        if isinstance(paths_payload, list):
            for path in paths_payload:
                if isinstance(path, str) and path:
                    self.context.add_output_file(path)

    def _handle_assistant_tools(self, data: dict[str, Any]) -> None:
        text = str(data.get("content") or "")
        if text:
            self.response_text = text
        tcs = data.get("tool_calls")
        rc = data.get("reasoning_content")
        reasoning = rc if isinstance(rc, str) and rc.strip() else None
        if isinstance(tcs, list):
            self.conversation.append_assistant_message(text, tool_calls=tcs, reasoning_content=reasoning)

    def _handle_assistant(self, data: dict[str, Any]) -> None:
        text = data.get("content") or ""
        if text and text not in self.response_text:
            self.response_text = text
        rc = data.get("reasoning_content")
        reasoning = rc if isinstance(rc, str) and rc.strip() else None
        self.conversation.append_assistant_message(text, reasoning_content=reasoning)

    def _handle_steer(self, data: dict[str, Any]) -> None:
        steer_text = str(data.get("content") or "")
        if steer_text:
            self.conversation.append_user_message(steer_text)

    async def _handle_result(
        self,
        data: dict[str, Any],
        *,
        turn_message_start: int,
    ) -> Any:
        reason = str(data.get("reason", "completed"))
        result_checkpoint_id = data.get("checkpoint_id")
        if isinstance(result_checkpoint_id, str) and result_checkpoint_id:
            self.last_checkpoint_id = result_checkpoint_id
            self._stash_checkpoint(self.context.session_id, result_checkpoint_id)
        usage = data.get("usage", {}) or {}

        if reason == TerminalReason.AWAITING_USER_INPUT.value:
            return await self._build_awaiting_user_response(
                data,
                usage,
                result_checkpoint_id,
                turn_message_start,
            )

        response = self.context.finalize_turn(
            text=self.response_text,
            reason=reason,
            conversation=self.conversation,
            turn_message_start=turn_message_start,
            error=data.get("error"),
            usage=usage,
            checkpoint_id=result_checkpoint_id,
            partial=reason != TerminalReason.COMPLETED.value,
        )
        await self.handler.on_complete(response)
        return response

    async def _build_awaiting_user_response(
        self,
        data: dict[str, Any],
        usage: dict[str, Any],
        result_checkpoint_id: str | None,
        turn_message_start: int,
    ) -> Any:
        from leagent.agent.query import (
            ASK_USER_PENDING_TOOL_JSON,
            ASK_USER_TOOL_NAME,
        )

        meta = data.get("meta") or {}
        tool_call = meta.get("tool_call") or {}
        questions = meta.get("questions") or []
        last_tcs: list[dict[str, Any]] | None = None
        if self.conversation.messages:
            last = self.conversation.messages[-1]
            if last.role == "assistant":
                last_tcs = last.tool_calls
        await self.handler.on_user_input_request(
            {
                "tool_call": tool_call,
                "questions": questions,
                "assistant_tool_calls": last_tcs,
                "checkpoint_id": result_checkpoint_id,
            },
        )
        _tid = str(tool_call.get("id") or "").strip()
        if _tid:
            self.conversation.append_tool_result(
                _tid,
                str(tool_call.get("name") or ASK_USER_TOOL_NAME),
                ASK_USER_PENDING_TOOL_JSON,
            )
        response = self.context.finalize_turn(
            text=self.response_text,
            reason=str(data.get("reason", "awaiting_user_input")),
            conversation=self.conversation,
            turn_message_start=turn_message_start,
            usage=usage,
            checkpoint_id=result_checkpoint_id,
            partial=True,
            metadata={
                "awaiting_user_input": True,
                "tool_call": tool_call,
                "questions": questions,
                "assistant_tool_calls": last_tcs,
                "checkpoint_id": result_checkpoint_id,
            },
        )
        await self.handler.on_complete(response)
        return response


def _tool_result_from_query_sdk(data: dict[str, Any]) -> ToolResult:
    """Reconstruct a :class:`ToolResult` from kernel tool_result data.

    Extracted from AgentController so the event mapper is self-contained.
    """
    env = data.get("envelope")
    if isinstance(env, dict):
        try:
            base = SimpleNamespace(
                success=bool(env.get("success", True)),
                data=env.get("data"),
                error=env.get("error"),
                metadata=dict(env.get("metadata") or {}),
                duration_ms=int(env.get("duration_ms", 0) or 0),
            )
            return ToolResult.from_base(
                base,
                tool_call_id=str(data.get("tool_use_id", "")),
                name=str(data.get("name", "")),
            )
        except Exception:  # noqa: BLE001
            logger.debug("query_sdk_envelope_reconstruct_failed", exc_info=True)
    tool_content = str(data.get("content", ""))
    is_success = bool(data.get("success", True))
    if is_success:
        return ToolResult(
            tool_call_id=str(data.get("tool_use_id", "")),
            name=str(data.get("name", "")),
            success=True,
            data=tool_content,
        )
    error_text = tool_content
    if error_text.startswith("Error: "):
        error_text = error_text[7:]
    return ToolResult(
        tool_call_id=str(data.get("tool_use_id", "")),
        name=str(data.get("name", "")),
        success=False,
        error=error_text or "Unknown error",
    )


__all__ = ["KernelEventMapper"]
