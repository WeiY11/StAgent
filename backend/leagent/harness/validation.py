"""Validation closure — pre/post-edit validation guards."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from typing import Any

import structlog

from leagent.harness.models import ValidationResult

logger = structlog.get_logger(__name__)

# Type for validation check functions
ValidationCheck = Callable[[dict[str, Any]], Awaitable[ValidationResult] | ValidationResult]


class ValidationClosure:
    """Pre/post-edit validation guards for agent operations.

    Provides a registry of validation checks that run before and after
    tool executions or code edits. Checks can gate execution (pre-edit)
    or flag regressions (post-edit).

    Usage:
        closure = ValidationClosure()
        closure.register_pre("syntax_check", check_syntax)
        closure.register_post("test_pass", check_tests)

        # Before edit
        pre_results = await closure.run_pre(context)
        if not closure.all_passed(pre_results):
            abort()

        # After edit
        post_results = await closure.run_post(context)
    """

    def __init__(self) -> None:
        self._pre_checks: dict[str, ValidationCheck] = {}
        self._post_checks: dict[str, ValidationCheck] = {}

    def register_pre(self, name: str, check: ValidationCheck) -> None:
        """Register a pre-edit validation check."""
        self._pre_checks[name] = check

    def register_post(self, name: str, check: ValidationCheck) -> None:
        """Register a post-edit validation check."""
        self._post_checks[name] = check

    def unregister(self, name: str) -> None:
        """Remove a check by name."""
        self._pre_checks.pop(name, None)
        self._post_checks.pop(name, None)

    async def run_pre(self, context: dict[str, Any]) -> list[ValidationResult]:
        """Run all pre-edit validation checks."""
        return await self._run_checks(self._pre_checks, context, phase="pre")

    async def run_post(self, context: dict[str, Any]) -> list[ValidationResult]:
        """Run all post-edit validation checks."""
        return await self._run_checks(self._post_checks, context, phase="post")

    async def run_all(self, context: dict[str, Any]) -> list[ValidationResult]:
        """Run both pre and post checks (useful for full validation)."""
        all_checks = {**self._pre_checks, **self._post_checks}
        return await self._run_checks(all_checks, context, phase="all")

    @staticmethod
    def all_passed(results: list[ValidationResult]) -> bool:
        """Check if all validation results passed."""
        return all(r.passed for r in results)

    @staticmethod
    def failures(results: list[ValidationResult]) -> list[ValidationResult]:
        """Extract failed validation results."""
        return [r for r in results if not r.passed]

    async def _run_checks(
        self,
        checks: dict[str, ValidationCheck],
        context: dict[str, Any],
        phase: str = "",
    ) -> list[ValidationResult]:
        """Execute checks concurrently and collect results."""
        if not checks:
            return []

        results: list[ValidationResult] = []
        tasks: list[tuple[str, Awaitable[ValidationResult]]] = []

        for name, check in checks.items():
            try:
                result = check(context)
                if inspect.isawaitable(result):
                    tasks.append((name, result))
                else:
                    results.append(result)
            except Exception as exc:  # noqa: BLE001
                results.append(
                    ValidationResult(
                        passed=False,
                        check_name=name,
                        message=f"Check raised: {exc}",
                    )
                )

        # Await async checks concurrently
        if tasks:
            gathered = await asyncio.gather(
                *[t[1] for t in tasks], return_exceptions=True
            )
            for (name, _), gathered_result in zip(tasks, gathered, strict=True):
                if isinstance(gathered_result, asyncio.CancelledError):
                    raise gathered_result
                if isinstance(gathered_result, BaseException):
                    results.append(
                        ValidationResult(
                            passed=False,
                            check_name=name,
                            message=f"Async check raised: {gathered_result}",
                        )
                    )
                else:
                    results.append(gathered_result)

        # Log summary
        passed = sum(1 for r in results if r.passed)
        failed = len(results) - passed
        if failed:
            logger.warning(
                "harness_validation_failures",
                phase=phase,
                passed=passed,
                failed=failed,
                failed_checks=[r.check_name for r in results if not r.passed],
            )
        else:
            logger.debug("harness_validation_passed", phase=phase, count=passed)

        return results


# ── Built-in validation checks ──────────────────────────────────────────────


async def check_no_destructive_ops(context: dict[str, Any]) -> ValidationResult:
    """Ensure no destructive operations (rm, drop, delete) are pending."""
    tool_name = context.get("tool_name", "")
    params = context.get("tool_params", {})
    raw = str(params).lower()

    destructive_patterns = ["rm -rf", "drop table", "delete from", "format", "truncate"]
    for pattern in destructive_patterns:
        if pattern in raw:
            return ValidationResult(
                passed=False,
                check_name="no_destructive_ops",
                message=f"Destructive pattern detected: '{pattern}' in {tool_name}",
                details={"tool": tool_name, "pattern": pattern},
            )
    return ValidationResult(passed=True, check_name="no_destructive_ops")


async def check_file_scope(context: dict[str, Any]) -> ValidationResult:
    """Ensure file operations stay within allowed workspace."""
    file_path = context.get("file_path", "")
    workspace = context.get("workspace_root", "")
    if not file_path or not workspace:
        return ValidationResult(passed=True, check_name="file_scope")

    from pathlib import Path

    try:
        resolved = Path(file_path).resolve()
        workspace_resolved = Path(workspace).resolve()
        if not str(resolved).startswith(str(workspace_resolved)):
            return ValidationResult(
                passed=False,
                check_name="file_scope",
                message=f"Path escapes workspace: {file_path}",
                details={"path": file_path, "workspace": workspace},
            )
    except (OSError, ValueError):
        pass
    return ValidationResult(passed=True, check_name="file_scope")


__all__ = [
    "ValidationClosure",
    "ValidationCheck",
    "check_file_scope",
    "check_no_destructive_ops",
]
