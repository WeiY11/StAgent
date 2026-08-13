"""Mission runner — the outer loop above the kernel think-act path.

:class:`MissionRunner` owns the mission state machine:
plan → wave-scheduled steps → one bounded *segment* per step (injected
:class:`~leagent.mission.segment.KernelSegmentExecutor`, wired to
``AgentRuntime.stream`` / ``run_loop``) → verification gate
(:class:`~leagent.mission.verifier.HeuristicVerifier`) → bounded
continuation. Every transition is persisted through
:class:`~leagent.db.repositories.missions.MissionRepository` and
published through an optional ``event_sink``.

Planning is injected as :class:`~leagent.mission.planner.LLMMissionPlanner`
(reusing the shared ``agent.planner`` prompt templates); ``readonly``
autonomy is enforced by the segment executor via a tool-registry scoped
allow-list.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol

from leagent.db.models.mission import (
    Mission,
    MissionAutonomy,
    MissionStatus,
    MissionStep,
    MissionStepKind,
    MissionStepStatus,
)
from leagent.mission.types import (
    MissionBudget,
    MissionPlan,
    MissionStepState,
    MissionUsage,
    SegmentResult,
    VerificationResult,
    parse_depends,
)
from leagent.utils.logging import get_logger

if TYPE_CHECKING:
    from uuid import UUID

    from leagent.db.repositories.missions import MissionRepository

logger = get_logger(__name__)

EventSink = Callable[[str, dict[str, Any]], Awaitable[None]]

_RESUMABLE_STATUSES = frozenset({MissionStatus.READY, MissionStatus.PAUSED, MissionStatus.RUNNING})
_PAUSEABLE_STATUSES = frozenset({MissionStatus.READY, MissionStatus.RUNNING})
_SEGMENT_SUMMARY_LIMIT = 4000


class MissionPlanner(Protocol):
    """Produces the mission plan. ``replan`` is optional (checked at runtime)."""

    async def plan(self, mission: Mission) -> MissionPlan:
        """Generate the initial plan for *mission*."""
        ...


class SegmentExecutor(Protocol):
    """Runs one bounded segment (one ``run_loop`` invocation) for a step."""

    async def execute(
        self,
        *,
        mission: Mission,
        step: MissionStep,
        prompt: str,
        attempt: int,
    ) -> SegmentResult: ...


class MissionVerifier(Protocol):
    """Applies the verification gate to a completed segment."""

    async def verify(
        self,
        *,
        mission: Mission,
        step: MissionStep,
        result: SegmentResult,
    ) -> VerificationResult: ...


class MissionContinuer(Protocol):
    """Builds the continuation prompt for a budget-exhausted segment."""

    async def continue_prompt(
        self,
        *,
        mission: Mission,
        step: MissionStep,
        result: SegmentResult,
    ) -> str: ...


class StaticPlanner:
    """Planner returning a fixed plan (tests + P1 wiring)."""

    def __init__(self, plan: MissionPlan, *, replan: MissionPlan | None = None) -> None:
        self._plan = plan
        self._replan = replan

    async def plan(self, mission: Mission) -> MissionPlan:
        return self._plan

    async def replan(self, mission: Mission, failed_step: MissionStep) -> MissionPlan | None:
        return self._replan


class AlwaysPassVerifier:
    """Default verification gate: accept every segment."""

    async def verify(
        self,
        *,
        mission: Mission,
        step: MissionStep,
        result: SegmentResult,
    ) -> VerificationResult:
        return VerificationResult(passed=True)


class IdentityContinuer:
    """Default continuer: re-issue the step with a continuation directive."""

    async def continue_prompt(
        self,
        *,
        mission: Mission,
        step: MissionStep,
        result: SegmentResult,
    ) -> str:
        return (
            f"继续步骤 {step.step_no}({step.description})。上一个执行段因预算耗尽而中断,请基于已有进展继续完成该步骤。"
        )


class StepOutcome(StrEnum):
    """Result of one step within the mission loop."""

    PASSED = "passed"
    FAILED = "failed"
    PAUSED = "paused"


class MissionRunner:
    """Durable goal-scoped execution state machine.

    Args:
        store: Mission persistence repository.
        planner: Produces (and optionally re-plans) the mission plan.
        executor: Runs one bounded segment per step.
        verifier: Verification gate (defaults to always-pass).
        continuer: Continuation prompt builder (defaults to identity).
        event_sink: Optional async ``(event, data)`` publisher.
    """

    def __init__(
        self,
        *,
        store: MissionRepository,
        planner: MissionPlanner,
        executor: SegmentExecutor,
        verifier: MissionVerifier | None = None,
        continuer: MissionContinuer | None = None,
        event_sink: EventSink | None = None,
    ) -> None:
        self._store = store
        self._planner = planner
        self._executor = executor
        self._verifier = verifier if verifier is not None else AlwaysPassVerifier()
        self._continuer = continuer if continuer is not None else IdentityContinuer()
        self._event_sink = event_sink

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def start(self, mission: Mission) -> Mission:
        """Generate and persist the plan; ``PLANNING → READY`` (or FAILED)."""
        if mission.status != MissionStatus.PLANNING:
            raise ValueError(f"cannot start mission in status {mission.status}")
        budget = MissionBudget.from_json(mission.budget_json)
        plan = await self._planner.plan(mission)
        errors = plan.validate(max_steps=budget.max_steps)
        if errors:
            mission.status = MissionStatus.FAILED
            mission.error = "invalid plan: " + "; ".join(errors)
            mission.completed_at = _now()
            await self._store.update(mission)
            await self._emit(
                "mission_plan_rejected",
                {"mission_id": str(mission.id), "errors": errors},
            )
            return mission
        plan.waves()  # assign wave numbers onto the step states
        await self._store.replace_steps(
            mission.id,
            mission.plan_id,
            [_step_to_row(mission.id, mission.plan_id, s) for s in plan.steps],
        )
        mission.expected_output = plan.expected_output
        mission.status = MissionStatus.READY
        await self._store.update(mission)
        await self._emit(
            "mission_planned",
            {"mission_id": str(mission.id), "steps": len(plan.steps)},
        )
        return mission

    async def run(self, mission: Mission) -> Mission:
        """Drive the mission loop until a terminal or paused state.

        Idempotent: plans ``PLANNING`` missions, resumes ``PAUSED`` ones,
        and recovers steps stuck in ``RUNNING`` (crash mid-segment) by
        resetting them to ``PENDING``.
        """
        mission = await self._store.get(mission.id) or mission
        if mission.status == MissionStatus.PLANNING:
            mission = await self.start(mission)
        if mission.status not in _RESUMABLE_STATUSES:
            return mission
        if mission.status != MissionStatus.RUNNING:
            await self._set_status(mission, MissionStatus.RUNNING)
        budget = MissionBudget.from_json(mission.budget_json)
        usage = MissionUsage.from_json(mission.usage_json)
        await self._recover_running_steps(mission)

        while True:
            mission = await self._store.get(mission.id) or mission
            if mission.status != MissionStatus.RUNNING:
                break
            if budget.expired():
                await self._finish(mission, MissionStatus.EXPIRED, reason="deadline")
                break

            steps = [
                s for s in await self._store.list_steps(mission.id, plan_id=mission.plan_id) if not _step_terminal(s)
            ]
            if not steps:
                await self._finish(mission, MissionStatus.COMPLETED)
                break

            # A step awaiting human approval blocks the mission until approved.
            waiting = next(
                (s for s in steps if s.status == MissionStepStatus.WAITING_APPROVAL),
                None,
            )
            if waiting is not None:
                await self._pause(mission, reason="step_confirm")
                break

            # The ready set is the remaining steps of the lowest wave:
            # earlier waves are terminal by construction, so every step
            # left at the lowest wave has its dependencies satisfied.
            current_wave = min(s.wave for s in steps)
            ready = sorted(
                (s for s in steps if s.wave == current_wave),
                key=lambda s: s.step_no,
            )
            for step_row in ready:
                if mission.status != MissionStatus.RUNNING:
                    break
                outcome = await self._run_step(mission, step_row, budget, usage)
                if outcome is StepOutcome.PAUSED:
                    break
                if outcome is StepOutcome.FAILED:
                    await self._maybe_replan(mission, step_row)
                    break
        return mission

    async def pause(self, mission: Mission, *, reason: str = "user") -> Mission:
        """Pause a running mission (graceful stop at the current step end)."""
        mission = await self._store.get(mission.id) or mission
        await self._pause(mission, reason=reason)
        return mission

    async def resume(self, mission: Mission) -> Mission:
        """Resume a paused mission (``PAUSED → RUNNING``)."""
        mission = await self._store.get(mission.id) or mission
        if mission.status != MissionStatus.PAUSED:
            raise ValueError(f"cannot resume mission in status {mission.status}")
        await self._set_status(mission, MissionStatus.RUNNING)
        await self._emit("mission_resumed", {"mission_id": str(mission.id)})
        return mission

    async def abort(self, mission: Mission) -> Mission:
        """Abort the mission (terminal)."""
        mission = await self._store.get(mission.id) or mission
        if mission.status in (
            MissionStatus.COMPLETED,
            MissionStatus.FAILED,
            MissionStatus.EXPIRED,
        ):
            return mission
        await self._finish(mission, MissionStatus.ABORTED)
        return mission

    async def approve_step(self, mission: Mission, step_no: int, *, approved: bool, note: str = "") -> Mission:
        """Approve or reject a ``WAITING_APPROVAL`` step (``step_confirm`` mode)."""
        mission = await self._store.get(mission.id) or mission
        step = await self._store.get_step(mission.id, step_no, plan_id=mission.plan_id)
        if step is None or step.status != MissionStepStatus.WAITING_APPROVAL:
            raise ValueError(f"step {step_no} is not awaiting approval")
        step.status = MissionStepStatus.PASSED if approved else MissionStepStatus.FAILED
        if not approved and note:
            step.verify_result = json.dumps({"passed": False, "feedback": note}, default=str)
        step.completed_at = _now()
        await self._store.upsert_step(step)
        if mission.status == MissionStatus.PAUSED:
            await self._set_status(mission, MissionStatus.RUNNING)
        await self._emit(
            "mission_step_approved",
            {"mission_id": str(mission.id), "step_no": step_no, "approved": approved},
        )
        return mission

    # ------------------------------------------------------------------
    # Step execution (segment → continuation → verification gate)
    # ------------------------------------------------------------------

    async def _run_step(
        self,
        mission: Mission,
        step: MissionStep,
        budget: MissionBudget,
        usage: MissionUsage,
    ) -> StepOutcome:
        step.status = MissionStepStatus.RUNNING
        step.started_at = _now()
        await self._store.upsert_step(step)
        await self._emit(
            "mission_step_started",
            {"mission_id": str(mission.id), "step_no": step.step_no},
        )

        prompt = await self._build_step_prompt(mission, step)
        segments_used = step.attempt  # carried over across resumes
        verify_retries = 0
        result: SegmentResult | None = None

        while segments_used < budget.max_segments_per_step:
            result = await self._safe_execute(mission, step, prompt, segments_used)
            if result is None:
                await self._fail_step(mission, step, "segment execution failed")
                return StepOutcome.FAILED
            segments_used += 1
            step.attempt = segments_used
            usage.add(result)
            mission.usage_json = usage.to_json()
            await self._store.update(mission)
            if usage.over(budget):
                await self._pause(mission, reason="budget")
                return StepOutcome.PAUSED
            if result.reason == "aborted":
                await self._finish(mission, MissionStatus.ABORTED)
                return StepOutcome.PAUSED
            if result.reason == "awaiting_user_input":
                # TODO(integration): on resume, feed the user's answer via the
                # saved checkpoint instead of re-running the step prompt.
                step.checkpoint_id = result.checkpoint_id
                await self._store.upsert_step(step)
                await self._pause(mission, reason="awaiting_user_input")
                return StepOutcome.PAUSED
            if result.continuable:
                await self._emit(
                    "mission_segment_continued",
                    {
                        "mission_id": str(mission.id),
                        "step_no": step.step_no,
                        "attempt": segments_used,
                    },
                )
                prompt = await self._continuer.continue_prompt(mission=mission, step=step, result=result)
                continue
            if not result.succeeded:
                await self._fail_step(mission, step, result.error or result.reason)
                return StepOutcome.FAILED

            step.status = MissionStepStatus.VERIFYING
            await self._store.upsert_step(step)
            await self._emit(
                "mission_step_verifying",
                {"mission_id": str(mission.id), "step_no": step.step_no},
            )
            verdict = await self._safe_verify(mission, step, result)
            if verdict is None:
                await self._fail_step(mission, step, "verification failed")
                return StepOutcome.FAILED
            step.verify_result = json.dumps(
                {
                    "passed": verdict.passed,
                    "feedback": verdict.feedback,
                    "evidence": verdict.evidence,
                },
                default=str,
            )
            if verdict.passed:
                await self._emit(
                    "mission_step_verified",
                    {"mission_id": str(mission.id), "step_no": step.step_no},
                )
                break
            verify_retries += 1
            await self._emit(
                "mission_step_verify_failed",
                {
                    "mission_id": str(mission.id),
                    "step_no": step.step_no,
                    "feedback": verdict.feedback,
                },
            )
            if verify_retries > budget.max_verify_retries:
                await self._fail_step(mission, step, f"verification failed: {verdict.feedback}")
                return StepOutcome.FAILED
            prompt = self._verify_feedback_prompt(step, verdict)
        else:
            await self._fail_step(mission, step, "segment budget exhausted")
            return StepOutcome.FAILED

        if result is None:  # pragma: no cover - guarded by the loop above
            await self._fail_step(mission, step, "no segment result")
            return StepOutcome.FAILED

        step.segment_summary = _truncate(result.text, _SEGMENT_SUMMARY_LIMIT)
        step.completed_at = _now()
        if mission.autonomy == MissionAutonomy.STEP_CONFIRM:
            step.status = MissionStepStatus.WAITING_APPROVAL
            await self._store.upsert_step(step)
            await self._emit(
                "mission_step_awaiting_approval",
                {"mission_id": str(mission.id), "step_no": step.step_no},
            )
            await self._pause(mission, reason="step_confirm")
            return StepOutcome.PAUSED
        step.status = MissionStepStatus.PASSED
        await self._store.upsert_step(step)
        await self._emit(
            "mission_step_passed",
            {"mission_id": str(mission.id), "step_no": step.step_no},
        )
        return StepOutcome.PASSED

    async def _maybe_replan(self, mission: Mission, failed_step: MissionStep) -> None:
        replanner = getattr(self._planner, "replan", None)
        if replanner is None:
            await self._pause(mission, reason="step_failed")
            return
        try:
            plan = await replanner(mission, failed_step)
        except Exception:
            logger.error(
                "mission_replan_failed",
                mission_id=str(mission.id),
                step_no=failed_step.step_no,
                exc_info=True,
            )
            plan = None
        if plan is None:
            await self._pause(mission, reason="step_failed")
            return
        budget = MissionBudget.from_json(mission.budget_json)
        if plan.validate(max_steps=budget.max_steps):
            await self._pause(mission, reason="step_failed")
            return
        new_plan_id = mission.plan_id + 1
        plan.waves()  # assign wave numbers onto the step states
        await self._store.replace_steps(
            mission.id,
            new_plan_id,
            [_step_to_row(mission.id, new_plan_id, s) for s in plan.steps],
        )
        mission.plan_id = new_plan_id
        await self._store.update(mission)
        await self._emit(
            "mission_replanned",
            {
                "mission_id": str(mission.id),
                "plan_id": new_plan_id,
                "failed_step_no": failed_step.step_no,
            },
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _build_step_prompt(self, mission: Mission, step: MissionStep) -> str:
        parts = [f"目标: {mission.goal}"]
        if mission.expected_output:
            parts.append(f"预期成果: {mission.expected_output}")
        all_steps = await self._store.list_steps(mission.id)
        parts.extend(
            f"上游步骤 {upstream.step_no} 结果: {upstream.segment_summary}"
            for upstream in all_steps
            if upstream.status == MissionStepStatus.PASSED
            and upstream.step_no in parse_depends(step.depends_on)
            and upstream.segment_summary
        )
        parts.append(f"当前步骤: {step.description}")
        return "\n\n".join(parts)

    def _verify_feedback_prompt(self, step: MissionStep, verdict: VerificationResult) -> str:
        return (
            f"步骤 {step.step_no}({step.description})未通过验证。\n"
            f"验证反馈: {verdict.feedback}\n"
            "请根据反馈修复问题后重新完成该步骤。"
        )

    async def _safe_execute(
        self, mission: Mission, step: MissionStep, prompt: str, attempt: int
    ) -> SegmentResult | None:
        try:
            return await self._executor.execute(mission=mission, step=step, prompt=prompt, attempt=attempt)
        except Exception:
            logger.error(
                "mission_segment_execution_failed",
                mission_id=str(mission.id),
                step_no=step.step_no,
                exc_info=True,
            )
            return None

    async def _safe_verify(
        self, mission: Mission, step: MissionStep, result: SegmentResult
    ) -> VerificationResult | None:
        try:
            return await self._verifier.verify(mission=mission, step=step, result=result)
        except Exception:
            logger.error(
                "mission_step_verification_failed",
                mission_id=str(mission.id),
                step_no=step.step_no,
                exc_info=True,
            )
            return None

    async def _recover_running_steps(self, mission: Mission) -> None:
        for step in await self._store.list_steps(mission.id, plan_id=mission.plan_id):
            if step.status in (MissionStepStatus.RUNNING, MissionStepStatus.VERIFYING):
                step.status = MissionStepStatus.PENDING
                await self._store.upsert_step(step)

    async def _fail_step(self, mission: Mission, step: MissionStep, error: str) -> None:
        step.status = MissionStepStatus.FAILED
        step.error = _truncate(error, _SEGMENT_SUMMARY_LIMIT)
        step.completed_at = _now()
        await self._store.upsert_step(step)
        await self._emit(
            "mission_step_failed",
            {
                "mission_id": str(mission.id),
                "step_no": step.step_no,
                "error": step.error,
            },
        )

    async def _set_status(self, mission: Mission, status: MissionStatus) -> None:
        mission.status = status
        await self._store.update(mission)

    async def _finish(self, mission: Mission, status: MissionStatus, *, reason: str | None = None) -> None:
        mission.status = status
        if reason:
            mission.pause_reason = reason
        mission.completed_at = _now()
        await self._store.update(mission)
        await self._emit(
            f"mission_{status.value}",
            {"mission_id": str(mission.id), "reason": reason},
        )

    async def _pause(self, mission: Mission, *, reason: str) -> None:
        if mission.status not in _PAUSEABLE_STATUSES:
            return
        mission.status = MissionStatus.PAUSED
        mission.pause_reason = reason
        await self._store.update(mission)
        await self._emit("mission_paused", {"mission_id": str(mission.id), "reason": reason})

    async def _emit(self, event: str, data: dict[str, Any]) -> None:
        if self._event_sink is None:
            return
        try:
            await self._event_sink(event, data)
        except Exception:
            logger.debug("mission_event_sink_failed", event=event, exc_info=True)


def _step_to_row(mission_id: UUID, plan_id: int, state: MissionStepState) -> MissionStep:
    """Convert a :class:`MissionStepState` into a persisted row."""
    return MissionStep(
        mission_id=mission_id,
        plan_id=plan_id,
        step_no=state.step_no,
        description=state.description,
        kind=_coerce_kind(state.kind),
        depends_on=json.dumps(state.depends_on),
        wave=state.wave,
        status=_coerce_status(state.status),
        attempt=state.attempt,
        segment_summary=state.segment_summary,
        checkpoint_id=state.checkpoint_id,
    )


def _coerce_kind(value: str) -> MissionStepKind:
    try:
        return MissionStepKind(value)
    except ValueError:
        return MissionStepKind.GENERIC


def _coerce_status(value: str) -> MissionStepStatus:
    try:
        return MissionStepStatus(value)
    except ValueError:
        return MissionStepStatus.PENDING


def _step_terminal(step: MissionStep) -> bool:
    return step.status in (
        MissionStepStatus.PASSED,
        MissionStepStatus.SKIPPED,
        MissionStepStatus.FAILED,
        MissionStepStatus.BLOCKED,
    )


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit]


__all__ = [
    "EventSink",
    "MissionRunner",
    "MissionPlanner",
    "SegmentExecutor",
    "MissionVerifier",
    "MissionContinuer",
    "StaticPlanner",
    "AlwaysPassVerifier",
    "IdentityContinuer",
    "StepOutcome",
]
