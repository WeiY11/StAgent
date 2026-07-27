"""Outcome evaluator — acceptance-criteria-based task outcome evaluation."""

from __future__ import annotations

from typing import Any

import structlog

from leagent.harness.models import (
    AcceptanceCriteria,
    EpisodeOutcome,
    FindingSeverity,
    TaskEpisode,
)

logger = structlog.get_logger(__name__)


class OutcomeEvaluator:
    """Evaluates whether a TaskEpisode meets its acceptance criteria.

    Evaluation rules:
    - All CRITICAL findings → FAIL regardless of criteria
    - All criteria met + no critical findings → PASS
    - Partial criteria met → PARTIAL
    - No criteria defined → infer from tool success rate + findings
    """

    # Thresholds
    TOOL_SUCCESS_THRESHOLD: float = 0.7
    MAX_CRITICAL_FINDINGS: int = 0
    MAX_HIGH_FINDINGS: int = 3
    PARTIAL_ACCEPTANCE_THRESHOLD: float = 0.5

    def evaluate(self, episode: TaskEpisode) -> EpisodeOutcome:
        """Evaluate episode outcome based on criteria and findings."""
        # Hard-fail on critical findings
        critical = sum(
            1 for f in episode.findings if f.severity == FindingSeverity.CRITICAL
        )
        if critical > self.MAX_CRITICAL_FINDINGS:
            logger.info(
                "harness_eval_fail_critical",
                episode_id=episode.id,
                critical_count=critical,
            )
            return EpisodeOutcome.FAIL

        # If criteria are defined, evaluate them
        if episode.criteria:
            return self._evaluate_criteria(episode)

        # No explicit criteria — infer from execution signals
        return self._infer_outcome(episode)

    def evaluate_criterion(
        self,
        criterion: AcceptanceCriteria,
        context: dict[str, Any],
    ) -> bool:
        """Evaluate a single acceptance criterion against execution context."""
        check_type = criterion.check_type

        if check_type == "tool_success":
            tool_name = criterion.check_params.get("tool_name", "")
            results = context.get("tool_results", [])
            for r in results:
                if r.get("name") == tool_name and r.get("success"):
                    criterion.met = True
                    criterion.evidence = f"Tool {tool_name} succeeded"
                    return True
            return False

        if check_type == "output_contains":
            pattern = criterion.check_params.get("pattern", "")
            output = context.get("output_text", "")
            if pattern and pattern.lower() in output.lower():
                criterion.met = True
                criterion.evidence = f"Output contains '{pattern}'"
                return True
            return False

        if check_type == "no_findings_above":
            max_severity = criterion.check_params.get(
                "severity", FindingSeverity.HIGH.value
            )
            findings = context.get("findings", [])
            severity_order = ["info", "low", "medium", "high", "critical"]
            threshold_idx = severity_order.index(max_severity)
            violations = [
                f
                for f in findings
                if severity_order.index(f.get("severity", "medium")) > threshold_idx
            ]
            if not violations:
                criterion.met = True
                criterion.evidence = f"No findings above {max_severity}"
                return True
            return False

        if check_type == "manual":
            # Manual criteria are pre-evaluated externally
            return criterion.met

        # Custom: delegate to check_params["evaluator"] if callable
        evaluator = criterion.check_params.get("evaluator")
        if callable(evaluator):
            result = evaluator(context)
            criterion.met = bool(result)
            return criterion.met

        return False

    def _evaluate_criteria(self, episode: TaskEpisode) -> EpisodeOutcome:
        """Evaluate based on explicit acceptance criteria."""
        total = len(episode.criteria)
        met = sum(1 for c in episode.criteria if c.met)

        if met == total:
            return EpisodeOutcome.PASS
        if met / total >= self.PARTIAL_ACCEPTANCE_THRESHOLD:
            return EpisodeOutcome.PARTIAL
        return EpisodeOutcome.FAIL

    def _infer_outcome(self, episode: TaskEpisode) -> EpisodeOutcome:
        """Infer outcome when no explicit criteria are defined."""
        # Count tool successes
        tool_success = 0
        tool_total = 0
        for r in episode.tool_results:
            tool_total += 1
            if r.get("success"):
                tool_success += 1

        # High-severity finding check
        high_count = sum(
            1 for f in episode.findings if f.severity == FindingSeverity.HIGH
        )
        if high_count > self.MAX_HIGH_FINDINGS:
            return EpisodeOutcome.FAIL

        # Tool success rate
        if tool_total > 0:
            rate = tool_success / tool_total
            if rate >= self.TOOL_SUCCESS_THRESHOLD:
                return EpisodeOutcome.PASS
            if rate >= self.PARTIAL_ACCEPTANCE_THRESHOLD:
                return EpisodeOutcome.PARTIAL
            return EpisodeOutcome.FAIL

        # No tools called and no critical findings → pass
        return EpisodeOutcome.PASS


__all__ = ["OutcomeEvaluator"]
