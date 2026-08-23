"""Findings tracker — structured issue capture during task execution."""

from __future__ import annotations

import threading
from typing import Any

import structlog

from StAgent.harness.models import Finding, FindingSeverity

logger = structlog.get_logger(__name__)


class FindingsTracker:
    """Thread-safe tracker for issues discovered during agent execution.

    Findings are categorized by severity and type, enabling
    quality-gated acceptance decisions and repair prioritization.
    """

    def __init__(self, episode_id: str = "") -> None:
        self._episode_id = episode_id
        self._findings: list[Finding] = []
        self._lock = threading.Lock()

    @property
    def findings(self) -> list[Finding]:
        with self._lock:
            return list(self._findings)

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._findings)

    @property
    def critical_count(self) -> int:
        with self._lock:
            return sum(
                1 for f in self._findings if f.severity == FindingSeverity.CRITICAL
            )

    @property
    def unresolved(self) -> list[Finding]:
        with self._lock:
            return [f for f in self._findings if not f.resolved]

    def record(
        self,
        title: str,
        *,
        severity: FindingSeverity = FindingSeverity.MEDIUM,
        category: str = "general",
        description: str = "",
        file_path: str | None = None,
        line_range: tuple[int, int] | None = None,
        tool_name: str | None = None,
        evidence: str = "",
        suggested_fix: str = "",
    ) -> Finding:
        """Record a new finding."""
        finding = Finding(
            severity=severity,
            category=category,
            title=title,
            description=description,
            file_path=file_path,
            line_range=line_range,
            tool_name=tool_name,
            evidence=evidence,
            suggested_fix=suggested_fix,
        )
        with self._lock:
            self._findings.append(finding)
        logger.info(
            "harness_finding_recorded",
            finding_id=finding.id,
            severity=severity.value,
            title=title[:80],
        )
        return finding

    def resolve(self, finding_id: str, evidence: str = "") -> bool:
        """Mark a finding as resolved with optional evidence."""
        with self._lock:
            for f in self._findings:
                if f.id == finding_id:
                    f.resolved = True
                    if evidence:
                        f.evidence = evidence
                    return True
        return False

    def from_tool_result(
        self,
        tool_name: str,
        result: dict[str, Any],
    ) -> Finding | None:
        """Auto-extract a finding from a failed tool result."""
        if result.get("success", True):
            return None
        error = str(result.get("error", ""))
        if not error:
            return None

        severity = self._infer_severity(error, tool_name)
        return self.record(
            title=f"Tool failure: {tool_name}",
            severity=severity,
            category="tool_failure",
            description=error[:500],
            tool_name=tool_name,
            evidence=str(result.get("data", ""))[:200],
        )

    def summary(self) -> dict[str, Any]:
        """Return a summary of all findings by severity."""
        with self._lock:
            by_severity: dict[str, int] = {}
            for f in self._findings:
                by_severity[f.severity.value] = by_severity.get(f.severity.value, 0) + 1
            return {
                "total": len(self._findings),
                "unresolved": sum(1 for f in self._findings if not f.resolved),
                "by_severity": by_severity,
                "critical": by_severity.get("critical", 0),
                "high": by_severity.get("high", 0),
            }

    def clear(self) -> None:
        with self._lock:
            self._findings.clear()

    @staticmethod
    def _infer_severity(error: str, tool_name: str) -> FindingSeverity:
        """Heuristic severity from error text."""
        lower = error.lower()
        if any(kw in lower for kw in ("security", "auth", "injection", "xss")):
            return FindingSeverity.CRITICAL
        if any(kw in lower for kw in ("timeout", "crash", "oom", "deadlock")):
            return FindingSeverity.HIGH
        if any(kw in lower for kw in ("deprecated", "warning", "slow")):
            return FindingSeverity.LOW
        return FindingSeverity.MEDIUM


__all__ = ["FindingsTracker"]
