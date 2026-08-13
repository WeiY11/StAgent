"""LLM mission planner — plan generation + replan prompts.

Reuses the proven prompt templates from :mod:`leagent.agent.planner`
(``PLAN_PROMPT_TEMPLATE`` / ``REPLAN_PROMPT_TEMPLATE``): the same structured
JSON is requested from the LLM, then parsed into the mission's
:class:`~leagent.mission.types.MissionPlan` working shape. Step ``kind`` is
inferred from the step description/tool so the heuristic verification gate
can specialise per category. Step ids are renumbered onto a clean ``1..N``
space (the LLM's ids are only used to resolve ``depends_on`` links), which
keeps ``plan_id``-scoped persistence and wave scheduling deterministic.

``replan`` mirrors :meth:`leagent.agent.planner.TaskPlanner.replan`: it
returns **only the remaining / adjusted steps** (never the completed ones),
so the runner can write them under ``plan_id + 1`` without duplicating
history.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import TYPE_CHECKING, Any

from leagent.agent.planner import PLAN_PROMPT_TEMPLATE, REPLAN_PROMPT_TEMPLATE
from leagent.db.models.mission import MissionStep, MissionStepStatus
from leagent.mission.types import MissionPlan, MissionStepState
from leagent.utils.logging import get_logger

if TYPE_CHECKING:
    from leagent.db.models.mission import Mission
    from leagent.db.repositories.missions import MissionRepository
    from leagent.llm import LLMService

logger = get_logger(__name__)

#: Kind classification keywords (first match wins), ordered most-specific
#: first. ``MissionStepKind`` values: code / research / data / file / generic.
_KIND_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("code", ("代码", "编码", "code", "实现", "开发", "编写", "重构", "修复", "测试", "test", "debug", "bug")),
    ("data", ("数据", "data", "分析", "图表", "统计", "清洗", "可视化", "csv", "excel")),
    ("research", ("调研", "调查", "搜索", "research", "search", "阅读", "总结", "汇总", "评估")),
    ("file", ("文件", "文档", "报告", "file", "doc", "report", "生成", "导出", "撰写")),
)

#: Tool note injected into the plan prompt: mission steps are executed by a
#: tool-selecting agent segment, so the planner is not asked to pick tools.
_PLAN_TOOL_NOTE = "(steps are executed by an agent that selects tools itself; leave tool/params null)"


class LLMMissionPlanner:
    """LLM-backed :class:`~leagent.mission.runner.MissionPlanner`.

    Args:
        llm: LLM service used for plan generation (``chat`` API).
        store: Optional mission repository read by :meth:`replan` to learn
            the current plan's completed steps (degraded replan when absent).
        abort_event: Optional shared abort signal for planning calls.
    """

    def __init__(
        self,
        llm: LLMService,
        *,
        store: MissionRepository | None = None,
        abort_event: asyncio.Event | None = None,
    ) -> None:
        self._llm = llm
        self._store = store
        self._abort_event = abort_event

    async def plan(self, mission: Mission) -> MissionPlan:
        """Generate the initial plan for *mission*."""
        prompt = PLAN_PROMPT_TEMPLATE.format(
            tool_summaries=_PLAN_TOOL_NOTE,
            relevant_knowledge="",
            applicable_rules="",
            task=mission.goal,
        )
        response = await self._chat([{"role": "user", "content": prompt}])
        data = _parse_plan_response(str(response.get("content") or ""))
        steps = _build_steps(data)
        _renumber_steps(steps)
        logger.info(
            "mission_plan_created",
            mission_id=str(mission.id),
            step_count=len(steps),
        )
        return MissionPlan(
            goal=str(data.get("goal") or mission.goal),
            expected_output=str(data.get("expected_output") or ""),
            steps=steps,
        )

    async def replan(self, mission: Mission, failed_step: MissionStep) -> MissionPlan | None:
        """Return an adjusted plan covering only the remaining work.

        Returns ``None`` when the LLM response cannot be parsed into usable
        steps (the runner then pauses the mission on ``step_failed``).
        """
        rows = await self._current_steps(mission)
        completed = [s for s in rows if s.status in (MissionStepStatus.PASSED, MissionStepStatus.SKIPPED)]
        completed_ids = sorted(s.step_no for s in completed)
        next_id = max((s.step_no for s in rows), default=0) + 1
        error = failed_step.error or "verification failed"

        prompt = REPLAN_PROMPT_TEMPLATE.format(
            step_id=failed_step.step_no,
            step_description=failed_step.description,
            error=error,
            goal=mission.goal,
            completed_steps=_format_completed_steps(completed),
            completed_ids=", ".join(str(i) for i in completed_ids) or "(none)",
            next_id=next_id,
        )
        response = await self._chat([{"role": "user", "content": prompt}])
        data = _parse_plan_response(str(response.get("content") or ""))
        steps = _build_steps(data)
        if not steps:
            logger.warning("mission_replan_empty", mission_id=str(mission.id))
            return None

        # Drop steps the LLM echoed that are already completed, and strip
        # dependency references into the completed set (the new plan only
        # covers pending work).
        completed_set = set(completed_ids)
        steps = [s for s in steps if s.step_no not in completed_set]
        for state in steps:
            state.depends_on = [d for d in state.depends_on if d not in completed_set]
        if not steps:
            logger.warning("mission_replan_all_completed", mission_id=str(mission.id))
            return None
        _renumber_steps(steps)
        logger.info(
            "mission_replan_created",
            mission_id=str(mission.id),
            failed_step_no=failed_step.step_no,
            step_count=len(steps),
        )
        return MissionPlan(goal=mission.goal, steps=steps)

    async def _current_steps(self, mission: Mission) -> list[MissionStep]:
        if self._store is None:
            return []
        try:
            return await self._store.list_steps(mission.id, plan_id=mission.plan_id)
        except Exception:  # noqa: BLE001 - replan degrades without store data
            logger.warning(
                "mission_replan_store_read_failed",
                mission_id=str(mission.id),
                exc_info=True,
            )
            return []

    async def _chat(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        """Run one planning chat call, honouring the optional abort event."""
        chat_task = asyncio.create_task(self._llm.chat(messages=messages, temperature=0.1))
        if self._abort_event is None:
            return await chat_task
        abort_task = asyncio.create_task(self._abort_event.wait())
        try:
            done, _pending = await asyncio.wait(
                {chat_task, abort_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            if not abort_task.done():
                abort_task.cancel()
        if abort_task in done and not chat_task.done():
            chat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):  # noqa: BLE001
                await chat_task
            raise asyncio.CancelledError("mission planner aborted")
        return await chat_task


# ---------------------------------------------------------------------------
# Parsing / building helpers
# ---------------------------------------------------------------------------


def _parse_plan_response(content: str) -> dict[str, Any]:
    """Parse an LLM plan response, tolerating markdown fences."""
    content = content.strip()
    if content.startswith("```"):
        lines = content.split("\n")
        if lines[-1].startswith("```"):
            lines = lines[:-1]
        lines = lines[1:]
        content = "\n".join(lines).strip()
        if content.startswith("json"):
            content = content[4:].strip()
    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        logger.warning("mission_plan_parse_failed", error=str(exc), content=content[:200])
        return {}
    return data if isinstance(data, dict) else {}


def _build_steps(data: dict[str, Any]) -> list[MissionStepState]:
    """Build ``MissionStepState`` s from the LLM JSON (kind inferred)."""
    raw_steps = data.get("steps")
    if not isinstance(raw_steps, list):
        return []
    steps: list[MissionStepState] = []
    for index, raw in enumerate(raw_steps):
        if not isinstance(raw, dict):
            continue
        description = str(raw.get("description") or "").strip()
        if not description:
            continue
        steps.append(
            MissionStepState(
                step_no=_to_int(raw.get("id"), index + 1),
                description=description,
                kind=_infer_kind(description, str(raw.get("tool") or "")),
                depends_on=_to_int_list(raw.get("depends_on")),
            )
        )
    return steps


def _renumber_steps(steps: list[MissionStepState]) -> None:
    """Renumber steps onto ``1..N`` in order, remapping ``depends_on`` links.

    The LLM's ids are arbitrary; a deterministic numbering keeps
    ``plan_id``-scoped persistence and wave scheduling stable. Dependency
    references that no longer resolve are dropped (the runner validates the
    plan anyway).
    """
    mapping: dict[int, int] = {}
    for index, state in enumerate(steps):
        mapping[state.step_no] = index + 1
        state.step_no = index + 1
    for state in steps:
        state.depends_on = [mapping[d] for d in state.depends_on if d in mapping]


def _infer_kind(description: str, tool: str) -> str:
    """Infer a ``MissionStepKind`` value from the description / tool name."""
    lower = (description + " " + tool).lower()
    for kind, keywords in _KIND_KEYWORDS:
        if any(keyword in lower for keyword in keywords):
            return kind
    return "generic"


def _format_completed_steps(completed: list[MissionStep]) -> str:
    if not completed:
        return "No steps completed yet."
    lines: list[str] = []
    for step in completed:
        preview = (step.segment_summary or "OK")[:100]
        lines.append(f"- Step {step.step_no}: {step.description} -> {preview}")
    return "\n".join(lines)


def _to_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_int_list(value: Any) -> list[int]:
    if not isinstance(value, list):
        return []
    return [item for item in (_to_int(v, 0) for v in value) if item > 0]


__all__ = ["LLMMissionPlanner"]
