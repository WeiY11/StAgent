"""Session scorer — quality scoring for task episodes."""

from __future__ import annotations

import structlog

from leagent.harness.models import (
    EpisodeOutcome,
    FindingSeverity,
    SessionScore,
    TaskEpisode,
)

logger = structlog.get_logger(__name__)


class SessionScorer:
    """Scores a TaskEpisode across multiple quality dimensions.

    Dimensions:
    - tool_success_rate: ratio of successful tool calls
    - finding_density: inverse ratio of findings per tool call (lower = better)
    - acceptance_rate: ratio of met acceptance criteria
    - efficiency: inverse of duration normalized to step count
    - severity_weight: penalty for high/critical findings
    """

    # Dimension weights (sum to 1.0)
    WEIGHTS: dict[str, float] = {
        "tool_success_rate": 0.30,
        "acceptance_rate": 0.30,
        "finding_density": 0.20,
        "severity_weight": 0.15,
        "efficiency": 0.05,
    }

    def score(self, episode: TaskEpisode) -> SessionScore:
        """Compute a comprehensive score for the episode."""
        dimensions: dict[str, float] = {}

        # Tool success rate
        tool_total = len(episode.tool_results)
        tool_success = sum(1 for r in episode.tool_results if r.get("success"))
        tool_failures = tool_total - tool_success
        dimensions["tool_success_rate"] = (
            tool_success / tool_total if tool_total > 0 else 1.0
        )

        # Acceptance rate
        dimensions["acceptance_rate"] = episode.acceptance_rate

        # Finding density (fewer findings per tool call = better)
        finding_count = len(episode.findings)
        if tool_total > 0:
            raw_density = finding_count / tool_total
            # Normalize: 0 findings = 1.0, 1 per call = 0.0
            dimensions["finding_density"] = max(0.0, 1.0 - raw_density)
        else:
            dimensions["finding_density"] = 1.0 if finding_count == 0 else 0.5

        # Severity weight penalty
        critical = sum(
            1 for f in episode.findings if f.severity == FindingSeverity.CRITICAL
        )
        high = sum(
            1 for f in episode.findings if f.severity == FindingSeverity.HIGH
        )
        severity_penalty = min(1.0, (critical * 0.4 + high * 0.15))
        dimensions["severity_weight"] = max(0.0, 1.0 - severity_penalty)

        # Efficiency: normalized step-duration ratio
        if episode.duration_ms > 0 and tool_total > 0:
            avg_ms_per_step = episode.duration_ms / tool_total
            # < 5s/step = excellent, > 60s/step = poor
            if avg_ms_per_step < 5000:
                dimensions["efficiency"] = 1.0
            elif avg_ms_per_step > 60000:
                dimensions["efficiency"] = 0.2
            else:
                dimensions["efficiency"] = 1.0 - (avg_ms_per_step - 5000) / 55000
        else:
            dimensions["efficiency"] = 0.8  # neutral default

        # Weighted overall score
        overall = sum(
            dimensions.get(dim, 0.0) * weight
            for dim, weight in self.WEIGHTS.items()
        )

        result = SessionScore(
            episode_id=episode.id,
            session_id=episode.session_id,
            overall=round(overall, 4),
            dimensions={k: round(v, 4) for k, v in dimensions.items()},
            findings_count=finding_count,
            critical_findings=critical,
            acceptance_met=sum(1 for c in episode.criteria if c.met),
            acceptance_total=len(episode.criteria),
            duration_ms=episode.duration_ms,
            tool_calls=tool_total,
            tool_failures=tool_failures,
            outcome=episode.outcome or EpisodeOutcome.PASS,
        )

        logger.info(
            "harness_session_scored",
            episode_id=episode.id,
            overall=result.overall,
            outcome=result.outcome.value,
        )
        return result


__all__ = ["SessionScorer"]
