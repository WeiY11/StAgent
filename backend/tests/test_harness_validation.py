"""Validation-closure contracts for mixed synchronous and asynchronous checks."""

from __future__ import annotations

import pytest

from leagent.harness.models import ValidationResult
from leagent.harness.validation import ValidationClosure


@pytest.mark.asyncio
async def test_validation_closure_runs_sync_and_async_checks() -> None:
    closure = ValidationClosure()
    closure.register_pre(
        "sync",
        lambda _context: ValidationResult(passed=True, check_name="sync"),
    )

    async def async_check(_context: dict[str, object]) -> ValidationResult:
        return ValidationResult(passed=True, check_name="async")

    closure.register_pre("async", async_check)

    results = await closure.run_pre({})

    assert {result.check_name for result in results} == {"sync", "async"}
    assert closure.all_passed(results)


@pytest.mark.asyncio
async def test_validation_closure_converts_async_exception_to_failure() -> None:
    closure = ValidationClosure()

    async def failing_check(_context: dict[str, object]) -> ValidationResult:
        raise RuntimeError("broken check")

    closure.register_post("failing", failing_check)

    results = await closure.run_post({})

    assert len(results) == 1
    assert results[0].passed is False
    assert results[0].check_name == "failing"
    assert "broken check" in results[0].message
