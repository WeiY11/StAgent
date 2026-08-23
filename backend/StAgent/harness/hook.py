"""HarnessHook — lifecycle hook integrating harness into the agent loop.

This hook sits in the agent's HookManager and:
1. Opens a TaskEpisode on agent start
2. Tracks tool calls and findings in real-time
3. Runs validation before/after tool execution
4. Evaluates outcome on completion
5. Scores the session and generates a durable report
6. Plans repairs on failure
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import structlog

from StAgent.agent.hooks import AgentHook
from StAgent.harness.evaluator import OutcomeEvaluator
from StAgent.harness.findings import FindingsTracker
from StAgent.harness.models import (
    EpisodeOutcome,
    EpisodeStatus,
    TaskEpisode,
)
from StAgent.harness.repair import RepairPlanner
from StAgent.harness.report import DurableReport
from StAgent.harness.scorer import SessionScorer
from StAgent.harness.validation import (
    ValidationClosure,
    check_file_scope,
    check_no_destructive_ops,
)

if TYPE_CHECKING:
    from pathlib import Path

    from StAgent.agent.base import (
        AgentContext,
        AgentResponse,
        ToolCall,
        ToolResult,
    )

logger = structlog.get_logger(__name__)


class HarnessHook(AgentHook):
    """Agent lifecycle hook that wraps execution in a TaskEpisode.

    Priority 25: runs after LoggingHook(10) and TraceHook(15) but
    before TaskHistoryHook(90), so findings are available for memory formation.
    """

    priority = 25
    name = "HarnessHook"

    def __init__(
        self,
        *,
        reports_dir: Path | None = None,
        enable_validation: bool = True,
        enable_reports: bool = True,
        auto_repair: bool = False,
    ) -> None:
        self._evaluator = OutcomeEvaluator()
        self._scorer = SessionScorer()
        self._repair_planner = RepairPlanner()
        self._report_gen = DurableReport(reports_dir=reports_dir)
        self._validation = ValidationClosure()
        self._enable_validation = enable_validation
        self._enable_reports = enable_reports
        self._auto_repair = auto_repair

        # Per-session episode tracking
        self._episodes: dict[str, TaskEpisode] = {}
        self._trackers: dict[str, FindingsTracker] = {}

        # Register default validation checks
        if enable_validation:
            self._validation.register_pre("no_destructive_ops", check_no_destructive_ops)
            self._validation.register_pre("file_scope", check_file_scope)

    # ── Properties ─────────────────────────────────────────────────────────

    @property
    def episodes(self) -> dict[str, TaskEpisode]:
        """Active episodes keyed by session_id."""
        return dict(self._episodes)

    def get_episode(self, session_id: str) -> TaskEpisode | None:
        """Get the current episode for a session."""
        return self._episodes.get(session_id)

    # ── Lifecycle callbacks ────────────────────────────────────────────────

    async def on_start(self, context: AgentContext, user_input: str) -> None:
        """Open a new TaskEpisode for this agent turn."""
        session_id = str(context.session_id or "")
        episode = TaskEpisode(
            session_id=session_id,
            user_id=str(context.user_id or ""),
            goal=user_input[:500],
            status=EpisodeStatus.RUNNING,
            started_at=time.time(),
        )
        self._episodes[session_id] = episode
        self._trackers[session_id] = FindingsTracker(episode_id=episode.id)

        logger.info(
            "harness_episode_started",
            episode_id=episode.id,
            session_id=session_id,
            goal=user_input[:80],
        )

    async def on_tool_call(self, context: AgentContext, tool_call: ToolCall) -> None:
        """Track tool call and run pre-edit validation."""
        session_id = str(context.session_id or "")
        episode = self._episodes.get(session_id)
        if not episode:
            return

        episode.tool_calls.append(tool_call.name)

        # Pre-validation
        if self._enable_validation:
            val_context = {
                "tool_name": tool_call.name,
                "tool_params": tool_call.arguments,
                "session_id": session_id,
                "workspace_root": context.extra.get("workspace_root", ""),
            }
            results = await self._validation.run_pre(val_context)
            for r in results:
                episode.validations.append(r)
            failures = self._validation.failures(results)
            if failures:
                tracker = self._trackers.get(session_id)
                if tracker:
                    for f in failures:
                        tracker.record(
                            title=f"Validation failed: {f.check_name}",
                            severity="high",
                            category="validation",
                            description=f.message,
                            tool_name=tool_call.name,
                        )

    async def on_tool_result(
        self,
        context: AgentContext,
        tool_call: ToolCall,
        result: ToolResult,
    ) -> None:
        """Track tool result and auto-extract findings from failures."""
        session_id = str(context.session_id or "")
        episode = self._episodes.get(session_id)
        if not episode:
            return

        result_dict = {
            "name": tool_call.name,
            "success": result.success,
            "error": result.error,
            "data": result.data,
            "duration_ms": result.duration_ms,
        }
        episode.tool_results.append(result_dict)

        # Auto-extract findings from failures
        if not result.success:
            tracker = self._trackers.get(session_id)
            if tracker:
                tracker.from_tool_result(tool_call.name, result_dict)

        # Post-validation
        if self._enable_validation:
            val_context = {
                "tool_name": tool_call.name,
                "tool_result": result_dict,
                "session_id": session_id,
            }
            post_results = await self._validation.run_post(val_context)
            for r in post_results:
                episode.validations.append(r)

    async def on_complete(self, context: AgentContext, response: AgentResponse) -> None:
        """Evaluate, score, and report the completed episode."""
        session_id = str(context.session_id or "")
        episode = self._episodes.get(session_id)
        if not episode:
            return

        episode.completed_at = time.time()

        # Collect findings into episode
        tracker = self._trackers.get(session_id)
        if tracker:
            episode.findings = tracker.findings

        # Evaluate outcome
        outcome = self._evaluator.evaluate(episode)
        episode.outcome = outcome
        episode.status = (
            EpisodeStatus.PASSED if outcome == EpisodeOutcome.PASS
            else EpisodeStatus.FAILED
        )

        # Score
        score = self._scorer.score(episode)
        episode.score = score

        # Generate repair plan on failure
        if outcome in (EpisodeOutcome.FAIL, EpisodeOutcome.PARTIAL):
            plan = self._repair_planner.plan(episode)
            if plan:
                episode.repair_plans.append(plan)

        # Persist report
        if self._enable_reports:
            try:
                self._report_gen.persist(episode)
            except Exception as exc:  # noqa: BLE001
                logger.warning("harness_report_persist_failed", error=str(exc))

        logger.info(
            "harness_episode_completed",
            episode_id=episode.id,
            outcome=outcome.value,
            score=score.overall,
            findings=len(episode.findings),
            duration_ms=episode.duration_ms,
        )

        # Cleanup
        self._episodes.pop(session_id, None)
        self._trackers.pop(session_id, None)

    async def on_error(self, context: AgentContext, error: Exception) -> None:
        """Mark episode as failed on unhandled error."""
        session_id = str(context.session_id or "")
        episode = self._episodes.get(session_id)
        if not episode:
            return

        episode.completed_at = time.time()
        episode.status = EpisodeStatus.FAILED
        episode.outcome = EpisodeOutcome.FAIL
        episode.metadata["error"] = str(error)[:500]

        # Persist report even on error
        if self._enable_reports:
            try:
                self._report_gen.persist(episode)
            except Exception as exc:  # noqa: BLE001
                logger.warning("harness_error_report_failed", error=str(exc))

        self._episodes.pop(session_id, None)
        self._trackers.pop(session_id, None)

    async def on_cancel(self, context: AgentContext) -> None:
        """Mark episode as cancelled."""
        session_id = str(context.session_id or "")
        episode = self._episodes.get(session_id)
        if not episode:
            return

        episode.completed_at = time.time()
        episode.status = EpisodeStatus.CANCELLED
        self._episodes.pop(session_id, None)
        self._trackers.pop(session_id, None)


__all__ = ["HarnessHook"]
