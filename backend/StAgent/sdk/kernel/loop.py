"""Kernel run loop — SDK wrapper around the existing ``query()`` loop.

This module bridges the proven ``StAgent.agent.query`` think-act-observe
generator (which emits ``ModelStreamEvent`` + ``ToolResultMessage``) with
the SDK's unified :class:`~StAgent.sdk.events.AgentEvent` taxonomy and
the :class:`~StAgent.sdk.kernel.state.RunState`.

The wrapping strategy is intentional: ``query()`` is complex, battle-tested
code with streaming salvage, recovery branches, and edge-case handling.
Rather than rewriting it, this module operates as a translation layer that:

1. Creates a ``RunState`` at run start.
2. Drives the ``QueryEngine.submit_message()`` loop.
3. Translates each ``SDKMessage`` → ``AgentEvent``.
4. Dispatches lifecycle hooks (``PreToolUse``/``PostToolUse`` equivalents)
   from a single site when a :class:`HookManager` + context are supplied.
5. Persists a :class:`Checkpoint` at turn boundaries (when a
   :class:`CheckpointStore` is provided), capturing the live message
   history so the run can be resumed.
6. Supports typed interrupts and resume via checkpoint IDs.

This is the single think-act path: every front end (chat ``AgentController``,
``AgentRuntime.stream``, sub-agent delegation) drives through ``run_loop``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from StAgent.sdk.events import AgentEvent, AgentEventType, AgentResult
from StAgent.sdk.kernel.state import RunState
from StAgent.utils.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from StAgent.agent.base import AgentContext
    from StAgent.agent.hooks import HookManager
    from StAgent.agent.query_engine import QueryEngine
    from StAgent.sdk.protocols import CheckpointStore, RunContext

logger = get_logger(__name__)

# Terminal reasons that should persist a durable checkpoint so the client
# can Continue / resume without replaying the whole turn from scratch.
# Normal ``completed`` turns are excluded unless ``checkpoint_on_complete``.
RESUMABLE_CHECKPOINT_REASONS: frozenset[str] = frozenset(
    {
        "awaiting_user_input",
        "max_turns",
        "token_budget_exceeded",
        "prompt_too_long",
        "aborted_streaming",
        "aborted",
        "blocking_limit",
        "model_error",
    }
)


def _run_context_from_engine(engine: QueryEngine) -> RunContext:
    """Build a typed :class:`RunContext` from a query engine's config."""
    from StAgent.sdk.protocols import RunContext

    cfg = engine.config
    return RunContext(
        abort_event=engine.abort_event,
        session_id=getattr(cfg, "session_id", None),
        user_id=getattr(cfg, "user_id", None),
        agent_id=getattr(cfg, "agent_id", None),
    )


def _snapshot_messages(engine: QueryEngine) -> list[dict[str, Any]]:
    """Snapshot the engine's live conversation for durable checkpointing.

    Without this the checkpoint would carry an empty transcript and a
    resumed run could not rebuild the conversation (the gap that made the
    checkpoint store write-only).
    """
    try:
        return [dict(m) for m in (engine.mutable_messages or [])]
    except Exception:  # noqa: BLE001 - never let snapshotting break a turn
        return []


async def _dispatch_tool_call(
    hooks: HookManager | None,
    ctx: AgentContext | None,
    data: dict[str, Any],
) -> None:
    """Fire the ``PreToolUse`` (``on_tool_call``) hook from the kernel."""
    if hooks is None or ctx is None:
        return
    from StAgent.agent.base import ToolCall

    tc = ToolCall(
        id=str(data.get("id") or ""),
        name=str(data.get("name") or ""),
        arguments=data.get("input") if isinstance(data.get("input"), dict) else {},
    )
    await hooks.dispatch_tool_call(ctx, tc)


async def _dispatch_tool_result(
    hooks: HookManager | None,
    ctx: AgentContext | None,
    data: dict[str, Any],
) -> None:
    """Fire the ``PostToolUse`` (``on_tool_result``) hook from the kernel."""
    if hooks is None or ctx is None:
        return
    from StAgent.agent.base import ToolCall, ToolResult

    tcid = str(data.get("tool_use_id") or data.get("tool_call_id") or "")
    name = str(data.get("name") or "")
    tc = ToolCall(id=tcid, name=name)
    res = ToolResult(
        tool_call_id=tcid,
        name=name,
        success=bool(data.get("success", True)),
        data=data.get("content"),
        error=data.get("error"),
        duration_ms=int(data.get("duration_ms") or 0),
    )
    await hooks.dispatch_tool_result(ctx, tc, res)


async def run_loop(
    engine: QueryEngine,
    prompt: str | dict[str, Any],
    *,
    run_state: RunState | None = None,
    run_context: RunContext | None = None,
    checkpoint_store: CheckpointStore | None = None,
    hooks: HookManager | None = None,
    hook_context: AgentContext | None = None,
    checkpoint_on_complete: bool = False,
    **submit_kwargs: Any,
) -> AsyncIterator[AgentEvent]:
    """Drive the query engine and yield unified :class:`AgentEvent` s.

    This is the kernel's primary async generator. It wraps
    ``engine.submit_message()`` and translates the SDK message stream
    into the canonical event taxonomy, while maintaining a
    :class:`RunState` that can be checkpointed. A :class:`RunContext`
    provides the typed per-run identity/abort handle (Codex-style turn
    context) used for checkpointing.

    ``hooks`` + ``hook_context`` opt the run into single-site lifecycle
    hook dispatch (``on_tool_call`` / ``on_tool_result``). ``submit_kwargs``
    (e.g. ``append_user_turn``) are forwarded to ``engine.submit_message``.
    """
    rctx = run_context or _run_context_from_engine(engine)
    state = run_state or RunState(
        session_id=str(rctx.session_id or ""),
        agent_name=str(rctx.agent_id or ""),
    )

    # Resolve the trace recorder once: doing the import + singleton lookup
    # per yielded event (stream deltas fire thousands of times per turn)
    # is measurable overhead on the SSE path.
    trace_on_event: Any = None
    try:
        from StAgent.telemetry.trace import get_trace_recorder

        trace_on_event = get_trace_recorder().on_event
    except Exception:
        logger.debug("agent_trace_init_failed", exc_info=True)

    async for sdk_msg in engine.submit_message(prompt, **submit_kwargs):
        event = AgentEvent.from_sdk_message(sdk_msg)

        etype = event.type
        data = event.data or {}

        if etype == AgentEventType.TOOL_USE:
            state.tool_calls_total += 1
            await _dispatch_tool_call(hooks, hook_context, data)
        elif etype == AgentEventType.TOOL_RESULT:
            await _dispatch_tool_result(hooks, hook_context, data)
        elif etype == AgentEventType.WORKSPACE_ATTACHMENTS:
            for path in data.get("paths") or []:
                if path:
                    state.produced_files.append(str(path))
        elif etype == AgentEventType.RESULT:
            state.reason = str(data.get("reason") or "completed")
            state.error = data.get("error")
            state.usage = dict(data.get("usage") or {})
            state.turn = int(data.get("turn_count", state.turn))
            # Capture the live transcript so a saved checkpoint can be
            # resumed with full history (not an empty message list).
            state.messages = _snapshot_messages(engine)

            should_checkpoint = checkpoint_store is not None and (
                state.reason in RESUMABLE_CHECKPOINT_REASONS
                or (checkpoint_on_complete and state.reason == "completed")
            )
            if should_checkpoint and checkpoint_store is not None:
                from StAgent.sdk.kernel.checkpoint import create_checkpoint

                cp = create_checkpoint(
                    session_id=state.session_id,
                    agent_name=state.agent_name,
                    turn=state.turn,
                    messages=state.messages,
                    reason=state.reason,
                    usage=state.usage,
                )
                await checkpoint_store.save(cp)
                event.data["checkpoint_id"] = cp.checkpoint_id
                try:
                    from StAgent.runtime.run_store import get_run_store

                    store = get_run_store()
                    exec_run = store.get_active_chat_turn(state.session_id)
                    if exec_run is not None:
                        token = exec_run.pause(
                            reason=state.reason,
                            checkpoint_id=cp.checkpoint_id,
                        )
                        # Persist the mutation so a shared (cross-worker) store
                        # sees the pause; a no-op re-store for the in-memory one.
                        store.register(exec_run)
                        event.data["pause_token"] = token.to_dict()
                except Exception:
                    logger.debug(
                        "execution_pause_registration_failed",
                        session_id=state.session_id,
                        exc_info=True,
                    )

        if trace_on_event is not None:
            try:
                trace_on_event(event)
            except Exception:
                logger.debug("agent_trace_on_event_failed", exc_info=True)

        yield event


async def run_to_result(
    engine: QueryEngine,
    prompt: str | dict[str, Any],
    *,
    run_state: RunState | None = None,
    checkpoint_store: CheckpointStore | None = None,
    collect_events: bool = False,
    **submit_kwargs: Any,
) -> AgentResult:
    """Drive the loop to completion and return an aggregate result."""
    text_parts: list[str] = []
    final_text = ""
    tool_calls = 0
    produced_files: list[str] = []
    events: list[AgentEvent] = []
    state = run_state or RunState()
    result = AgentResult(session_id=state.session_id)

    async for event in run_loop(
        engine, prompt,
        run_state=state,
        checkpoint_store=checkpoint_store,
        **submit_kwargs,
    ):
        if collect_events:
            events.append(event)

        etype = event.type
        data = event.data or {}
        if etype == AgentEventType.STREAM_DELTA:
            delta = data.get("content")
            if delta:
                text_parts.append(str(delta))
        elif etype == AgentEventType.ASSISTANT:
            final_text = str(data.get("content") or "")
        elif etype == AgentEventType.TOOL_USE:
            tool_calls += 1
        elif etype == AgentEventType.WORKSPACE_ATTACHMENTS:
            for path in data.get("paths") or []:
                if path:
                    produced_files.append(str(path))
        elif etype == AgentEventType.RESULT:
            result.session_id = str(data.get("session_id") or result.session_id)
            result.reason = str(data.get("reason") or "completed")
            result.error = data.get("error")
            result.usage = dict(data.get("usage") or {})
            result.meta = dict(data.get("meta") or {})

    result.text = final_text or "".join(text_parts)
    result.tool_calls = tool_calls
    result.produced_files = produced_files
    result.events = events
    return result


__all__ = ["RESUMABLE_CHECKPOINT_REASONS", "run_loop", "run_to_result"]
