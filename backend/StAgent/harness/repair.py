"""Repair planner — corrective action planning on task failure."""

from __future__ import annotations

from typing import Any

import structlog

from StAgent.harness.models import (
    EpisodeOutcome,
    Finding,
    FindingSeverity,
    RepairAction,
    RepairPlan,
    TaskEpisode,
)

logger = structlog.get_logger(__name__)


class RepairPlanner:
    """Generates structured repair plans when a TaskEpisode fails.

    The planner analyzes findings and failed criteria to produce
    a prioritized list of corrective actions the agent can execute.
    """

    def plan(self, episode: TaskEpisode) -> RepairPlan | None:
        """Generate a repair plan for a failed episode.

        Returns None if the episode passed or repair is not actionable.
        """
        if episode.outcome == EpisodeOutcome.PASS:
            return None

        actions: list[RepairAction] = []

        # Priority 1: address critical/high findings
        for finding in self._prioritized_findings(episode):
            action = self._action_from_finding(finding)
            if action:
                actions.append(action)

        # Priority 2: retry failed tool calls
        for tool_result in episode.tool_results:
            if not tool_result.get("success"):
                action = self._action_from_failed_tool(tool_result)
                if action:
                    actions.append(action)

        # Priority 3: unmet acceptance criteria
        for criterion in episode.criteria:
            if not criterion.met:
                action = RepairAction(
                    description=f"Address unmet criterion: {criterion.description}",
                    priority=20,
                )
                actions.append(action)

        if not actions:
            return None

        # Sort by priority (lower = higher priority)
        actions.sort(key=lambda a: a.priority)

        plan = RepairPlan(
            episode_id=episode.id,
            reason=self._summarize_failure(episode),
            actions=actions,
        )

        logger.info(
            "harness_repair_plan_created",
            episode_id=episode.id,
            action_count=len(actions),
        )
        return plan

    def apply_action(self, action: RepairAction, result: dict[str, Any]) -> None:
        """Mark a repair action as complete with its result."""
        action.status = "done" if result.get("success") else "skipped"
        action.result = str(result.get("message", ""))[:500]

    def _prioritized_findings(self, episode: TaskEpisode) -> list[Finding]:
        """Return unresolved findings sorted by severity."""
        severity_order = {
            FindingSeverity.CRITICAL: 0,
            FindingSeverity.HIGH: 1,
            FindingSeverity.MEDIUM: 2,
            FindingSeverity.LOW: 3,
            FindingSeverity.INFO: 4,
        }
        unresolved = [f for f in episode.findings if not f.resolved]
        return sorted(unresolved, key=lambda f: severity_order.get(f.severity, 5))

    def _action_from_finding(self, finding: Finding) -> RepairAction | None:
        """Convert a finding into a repair action."""
        priority = {
            FindingSeverity.CRITICAL: 0,
            FindingSeverity.HIGH: 5,
            FindingSeverity.MEDIUM: 10,
            FindingSeverity.LOW: 15,
            FindingSeverity.INFO: 20,
        }.get(finding.severity, 10)

        description = f"Fix [{finding.severity.value}] {finding.title}"
        if finding.suggested_fix:
            description += f": {finding.suggested_fix}"

        params: dict[str, Any] = {}
        if finding.file_path:
            params["file_path"] = finding.file_path
        if finding.line_range:
            params["line_range"] = finding.line_range

        return RepairAction(
            description=description,
            tool_name=finding.tool_name,
            tool_params=params,
            priority=priority,
        )

    def _action_from_failed_tool(self, tool_result: dict[str, Any]) -> RepairAction | None:
        """Convert a failed tool result into a retry action."""
        name = tool_result.get("name", "unknown")
        error = tool_result.get("error", "")
        if not error:
            return None
        return RepairAction(
            description=f"Retry tool '{name}': {error[:100]}",
            tool_name=name,
            tool_params=tool_result.get("params", {}),
            priority=10,
        )

    def _summarize_failure(self, episode: TaskEpisode) -> str:
        """Build a concise failure summary for the plan."""
        parts: list[str] = []
        critical = sum(
            1 for f in episode.findings if f.severity == FindingSeverity.CRITICAL
        )
        high = sum(
            1 for f in episode.findings if f.severity == FindingSeverity.HIGH
        )
        if critical:
            parts.append(f"{critical} critical finding(s)")
        if high:
            parts.append(f"{high} high finding(s)")

        unmet = sum(1 for c in episode.criteria if not c.met)
        if unmet:
            parts.append(f"{unmet} unmet criterion/criteria")

        failed_tools = sum(1 for r in episode.tool_results if not r.get("success"))
        if failed_tools:
            parts.append(f"{failed_tools} failed tool call(s)")

        return "; ".join(parts) if parts else "Episode did not meet acceptance threshold"


__all__ = ["RepairPlanner"]
