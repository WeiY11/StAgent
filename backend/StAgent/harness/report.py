"""Durable report generation for task episodes."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import structlog

from StAgent.harness.models import (
    EpisodeOutcome,
    TaskEpisode,
)

logger = structlog.get_logger(__name__)


class DurableReport:
    """Generates and persists structured reports for completed task episodes.

    Reports are stored as JSON files under StAgent_HOME/harness/reports/
    and can be queried via the harness API endpoint.
    """

    def __init__(self, reports_dir: Path | None = None) -> None:
        if reports_dir is None:
            from StAgent.config.constants import StAgent_HOME

            self._reports_dir = StAgent_HOME / "harness" / "reports"
        else:
            self._reports_dir = reports_dir
        self._reports_dir.mkdir(parents=True, exist_ok=True)

    def generate(self, episode: TaskEpisode) -> dict[str, Any]:
        """Generate a structured report from a completed episode."""
        report = {
            "report_id": f"rpt_{episode.id}",
            "episode_id": episode.id,
            "session_id": episode.session_id,
            "user_id": episode.user_id,
            "generated_at": time.time(),
            "goal": episode.goal,
            "status": episode.status.value,
            "outcome": episode.outcome.value if episode.outcome else None,
            "duration_ms": episode.duration_ms,
            "summary": self._build_summary(episode),
            "findings": [f.model_dump() for f in episode.findings],
            "criteria": [c.model_dump() for c in episode.criteria],
            "score": episode.score.model_dump() if episode.score else None,
            "repair_plans": [p.model_dump() for p in episode.repair_plans],
            "validations": [v.model_dump() for v in episode.validations],
            "tool_calls": episode.tool_calls,
            "metadata": episode.metadata,
        }
        return report

    def persist(self, episode: TaskEpisode) -> Path:
        """Generate and save a report to disk."""
        report = self.generate(episode)
        filename = f"{report['report_id']}_{int(time.time())}.json"
        filepath = self._reports_dir / filename

        filepath.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        logger.info(
            "harness_report_persisted",
            report_id=report["report_id"],
            path=str(filepath),
        )
        return filepath

    def list_reports(
        self,
        *,
        session_id: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """List persisted reports, optionally filtered by session."""
        reports: list[dict[str, Any]] = []
        if not self._reports_dir.exists():
            return reports

        files = sorted(self._reports_dir.glob("rpt_*.json"), reverse=True)
        for f in files[:limit * 2]:  # over-fetch for filter
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                if session_id and data.get("session_id") != session_id:
                    continue
                reports.append(data)
                if len(reports) >= limit:
                    break
            except (json.JSONDecodeError, OSError):
                continue
        return reports

    def get_report(self, report_id: str) -> dict[str, Any] | None:
        """Load a specific report by ID."""
        for f in self._reports_dir.glob(f"{report_id}_*.json"):
            try:
                return json.loads(f.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
        return None

    def _build_summary(self, episode: TaskEpisode) -> str:
        """Build a human-readable summary line."""
        parts: list[str] = []
        outcome_label = {
            EpisodeOutcome.PASS: "PASSED",
            EpisodeOutcome.FAIL: "FAILED",
            EpisodeOutcome.PARTIAL: "PARTIAL",
            EpisodeOutcome.BLOCKED: "BLOCKED",
        }
        if episode.outcome:
            parts.append(outcome_label.get(episode.outcome, episode.outcome.value))
        parts.append(f"goal='{episode.goal[:60]}'")

        if episode.findings:
            parts.append(f"{len(episode.findings)} finding(s)")
        if episode.criteria:
            met = sum(1 for c in episode.criteria if c.met)
            parts.append(f"criteria {met}/{len(episode.criteria)}")
        if episode.score:
            parts.append(f"score={episode.score.overall:.2f}")

        return " | ".join(parts)


__all__ = ["DurableReport"]
