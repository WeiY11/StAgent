"""Kernel segment executor — one bounded ``run_loop`` invocation per step.

Wires the mission loop's :class:`~StAgent.mission.runner.SegmentExecutor`
seam to the real model: each step runs through
``AgentRuntime.stream`` → ``run_loop``, using the same runtime construction
as :class:`StAgent.tasks.handlers.agent_handler.AgentTaskHandler` (a
registry-scoped :class:`~StAgent.tools.executor.ToolExecutor` plus
``AgentRuntime.from_service_manager``), so mission segments share
checkpoint / hook / ExecutionRun semantics with chat SSE and background
tasks.

The executor folds the run stream into a :class:`SegmentResult`: the
assistant transcript, produced files, tool activity (changed paths + shell
command blobs for the heuristic verifier), usage, and the terminal reason /
checkpoint id. ``MissionAutonomy.READONLY`` missions scope the registry to
``is_read_only`` tools only.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from StAgent.db.models.mission import Mission, MissionAutonomy, MissionStep
from StAgent.mission.types import SegmentResult
from StAgent.utils.logging import get_logger

if TYPE_CHECKING:
    from StAgent.runtime.runtime import AgentRuntime
    from StAgent.services.service_manager import ServiceManager

logger = get_logger(__name__)


class KernelSegmentExecutor:
    """Run one bounded mission segment through ``AgentRuntime.stream``.

    Args:
        service_manager: Optional :class:`ServiceManager`; the runtime is
            built lazily from it (the ``agent_handler`` construction path).
        runtime: Optional pre-built :class:`AgentRuntime` (tests / shared
            wiring); when absent one is built from ``service_manager``.
        agent_id: Fallback agent name when ``mission.agent_id`` is unset.
        runtime_profile: Optional runtime budget profile for the segment.
        max_turns: Optional per-segment turn cap (defaults to the profile).
        max_tool_calls_per_turn: Optional per-turn tool-call cap.
        abort_event: Optional shared abort signal bridged into the run.
    """

    def __init__(
        self,
        *,
        service_manager: ServiceManager | None = None,
        runtime: AgentRuntime | None = None,
        agent_id: str = "default_agent",
        runtime_profile: str | None = None,
        max_turns: int | None = None,
        max_tool_calls_per_turn: int | None = None,
        abort_event: asyncio.Event | None = None,
    ) -> None:
        self._sm = service_manager
        self._runtime = runtime
        self._agent_id = agent_id
        self._runtime_profile = runtime_profile
        self._max_turns = max_turns
        self._max_tool_calls_per_turn = max_tool_calls_per_turn
        self._abort_event = abort_event

    async def execute(
        self,
        *,
        mission: Mission,
        step: MissionStep,
        prompt: str,
        attempt: int,
    ) -> SegmentResult:
        """Run one bounded segment for *step* and fold the stream into a result."""
        from StAgent.sdk import AgentDefinition, MemoryPolicy, ToolPolicy
        from StAgent.sdk.events import AgentEventType

        runtime, budget_name = self._ensure_runtime()
        agent_name = mission.agent_id or self._agent_id
        definition = AgentDefinition(
            name=agent_name,
            prompt_variant=agent_name,
            runtime_profile=budget_name,
            max_turns=self._max_turns,
            max_tool_calls_per_turn=self._max_tool_calls_per_turn,
            memory=MemoryPolicy(enabled=False, formation=False),
            tools=ToolPolicy(allow=self._readonly_allow(mission.autonomy)),
        )
        tool_extra: dict[str, Any] = {
            "mission_id": str(mission.id),
            "step_no": step.step_no,
            "attempt": attempt,
        }

        text_parts: list[str] = []
        final_text = ""
        reason = "completed"
        error: str | None = None
        checkpoint_id: str | None = None
        usage: dict[str, Any] = {}
        produced: list[str] = []
        changed: set[str] = set()
        shell_blobs: list[str] = []
        activity: list[dict[str, Any]] = []

        try:
            async for event in runtime.stream(
                definition,
                prompt,
                session_id=mission.session_id,
                user_id=mission.user_id,
                tool_extra=tool_extra,
                abort_event=self._abort_event,
            ):
                etype = event.type
                data = event.data or {}
                if etype == AgentEventType.STREAM_DELTA:
                    delta = data.get("content")
                    if delta:
                        text_parts.append(str(delta))
                elif etype == AgentEventType.ASSISTANT:
                    content = data.get("content")
                    if content:
                        final_text = str(content)
                elif etype == AgentEventType.WORKSPACE_ATTACHMENTS:
                    produced.extend(str(path) for path in data.get("paths") or [] if path)
                elif etype == AgentEventType.TOOL_USE:
                    _record_tool_use(data, changed, shell_blobs, activity)
                elif etype == AgentEventType.TOOL_RESULT:
                    _record_tool_result(data, activity)
                elif etype == AgentEventType.RESULT:
                    reason = str(data.get("reason") or "completed")
                    error = data.get("error")
                    usage = dict(data.get("usage") or {})
                    cid = data.get("checkpoint_id")
                    if isinstance(cid, str) and cid:
                        checkpoint_id = cid
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a dead segment must not kill the loop
            logger.error(
                "mission_segment_stream_failed",
                mission_id=str(mission.id),
                step_no=step.step_no,
                exc_info=True,
            )
            reason = "model_error"
            error = str(exc) or type(exc).__name__

        return SegmentResult(
            reason=reason,
            text=final_text or "".join(text_parts),
            produced_files=produced,
            changed_paths=sorted(changed),
            shell_blobs=shell_blobs,
            activity=activity,
            usage=usage,
            error=error,
            checkpoint_id=checkpoint_id,
        )

    def _ensure_runtime(self) -> tuple[AgentRuntime, str]:
        """Build (once) the runtime + budget name, mirroring ``agent_handler``."""
        if self._runtime is not None:
            return self._runtime, self._runtime_profile or "standard"

        from StAgent.agent.runtime_profile import resolve_runtime_budget
        from StAgent.config.settings import get_settings
        from StAgent.runtime.runtime import AgentRuntime
        from StAgent.tools.executor import ToolExecutor
        from StAgent.tools.registry import get_registry

        sm = self._sm
        if sm is None:
            raise RuntimeError("KernelSegmentExecutor requires service_manager or an injected runtime")
        tool_registry = get_registry()
        settings = get_settings()
        budget = resolve_runtime_budget(self._runtime_profile, settings=settings)
        executor = ToolExecutor(
            registry=tool_registry,
            service_manager=sm,
            default_timeout=float(budget.tool_timeout_sec),
        )
        self._runtime = AgentRuntime.from_service_manager(sm, executor=executor)
        return self._runtime, budget.name

    def _readonly_allow(self, autonomy: MissionAutonomy) -> list[str]:
        """Tool allow-list for ``readonly`` autonomy (empty = no restriction)."""
        if autonomy != MissionAutonomy.READONLY:
            return []
        from StAgent.tools.registry import get_registry

        registry = get_registry()
        get_enabled = getattr(registry, "get_enabled_tools", None)
        tools = get_enabled() if callable(get_enabled) else registry.list_tools()
        return [tool.name for tool in tools if getattr(tool, "is_read_only", False)]


# ---------------------------------------------------------------------------
# Stream → SegmentResult collection helpers (mirror the sub-agent activity
# collector in ``StAgent.agent.subagent``).
# ---------------------------------------------------------------------------


def _paths_from_unified_diff(diff: str) -> list[str]:
    paths: list[str] = []
    for line in diff.splitlines():
        if line.startswith(("+++ ", "--- ")):
            raw = line[4:].strip()
            if raw in ("/dev/null",):
                continue
            normalized = raw.removeprefix("a/").removeprefix("b/").strip()
            if normalized:
                paths.append(normalized)
    return paths


def _primary_tool_path(tool_name: str, inp: dict[str, Any]) -> str | None:
    for key in ("path", "project_path"):
        value = inp.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    if tool_name == "project_apply_patch":
        diff = inp.get("diff")
        if isinstance(diff, str) and diff.strip():
            for path in _paths_from_unified_diff(diff):
                return path
    return None


def _shell_command_blob(inp: dict[str, Any]) -> str:
    """Flatten argv / shell into one searchable string."""
    parts: list[str] = []
    argv = inp.get("argv")
    if isinstance(argv, list):
        parts.append(" ".join(str(part) for part in argv))
    shell = inp.get("shell")
    if isinstance(shell, str) and shell.strip():
        parts.append(shell)
    return " ".join(parts).lower()


def _record_tool_use(
    data: dict[str, Any],
    changed: set[str],
    shell_blobs: list[str],
    activity: list[dict[str, Any]],
) -> None:
    name = str(data.get("name") or "")
    inp = data.get("input") or {}
    if not isinstance(inp, dict):
        inp = {}
    if name in ("project_write", "project_edit"):
        path = _primary_tool_path(name, inp)
        if path:
            changed.add(path)
    elif name == "project_apply_patch":
        diff = inp.get("diff")
        if isinstance(diff, str):
            for path in _paths_from_unified_diff(diff):
                changed.add(path)
    row: dict[str, Any] = {"tool": name}
    path = _primary_tool_path(name, inp)
    if path:
        row["path"] = path
    if name == "project_shell":
        shell_blobs.append(_shell_command_blob(inp))
    activity.append(row)


def _record_tool_result(data: dict[str, Any], activity: list[dict[str, Any]]) -> None:
    name = str(data.get("name") or "")
    if name == "project_shell" and activity:
        # Enrich the last row with the shell result summary (verifier reads
        # only the input blobs, this keeps the activity rows informative).
        last = activity[-1]
        content = str(data.get("content") or "")[:200].replace("\n", " ").strip()
        if content:
            last["summary"] = content


__all__ = ["KernelSegmentExecutor"]
