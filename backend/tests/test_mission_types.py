"""Working-shape tests for :mod:`leagent.mission.types` (no LLM, no DB)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from leagent.mission.types import (
    CONTINUABLE_SEGMENT_REASONS,
    MissionBudget,
    MissionPlan,
    MissionStepState,
    MissionUsage,
    SegmentResult,
)


def test_segment_cost_usd_reads_usage_payload():
    result = SegmentResult(usage={"total_tokens": 10, "cost_usd": "0.5"})
    assert result.cost_usd == 0.5
    assert SegmentResult(usage={"total_tokens": 10}).cost_usd == 0.0
    assert SegmentResult(usage={"cost_usd": "garbage"}).cost_usd == 0.0


def test_usage_add_folds_segments_tokens_and_cost():
    usage = MissionUsage()
    usage.add(SegmentResult(usage={"total_tokens": 100, "cost_usd": 0.25}))
    usage.add(SegmentResult(usage={"input_tokens": 20, "output_tokens": 30}))
    assert usage.segments == 2
    assert usage.tokens == 150
    assert usage.cost_usd == 0.25


def test_usage_over_checks_cost_cap():
    budget = MissionBudget(max_cost_usd=1.0)
    usage = MissionUsage()
    usage.add(SegmentResult(usage={"cost_usd": 1.5}))
    assert usage.over(budget)
    usage_no_cost = MissionUsage()
    usage_no_cost.add(SegmentResult(usage={"total_tokens": 10}))
    assert not usage_no_cost.over(budget)


def test_usage_json_round_trip_keeps_cost():
    usage = MissionUsage(segments=3, tokens=500, cost_usd=0.75)
    restored = MissionUsage.from_json(usage.to_json())
    assert restored.segments == 3
    assert restored.tokens == 500
    assert restored.cost_usd == 0.75


def test_budget_json_round_trip_with_deadline():
    deadline = datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=1)
    budget = MissionBudget(max_steps=5, max_cost_usd=2.5, deadline=deadline)
    restored = MissionBudget.from_json(budget.to_json())
    assert restored.max_steps == 5
    assert restored.max_cost_usd == 2.5
    assert restored.deadline == deadline
    assert not restored.expired()


def test_budget_expired_naive_utc_comparison():
    past = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=1)
    future = datetime.now(UTC).replace(tzinfo=None) + timedelta(minutes=1)
    assert MissionBudget(deadline=past).expired()
    assert not MissionBudget(deadline=future).expired()
    assert not MissionBudget().expired()


def test_plan_validate_duplicate_step_no():
    plan = MissionPlan(
        goal="g",
        steps=[
            MissionStepState(step_no=1, description="a"),
            MissionStepState(step_no=1, description="b"),
        ],
    )
    errors = plan.validate(max_steps=20)
    assert any("duplicate step_no 1" in error for error in errors)


def test_plan_waves_reject_invalid_plan():
    cyclic = MissionPlan(
        goal="g",
        steps=[
            MissionStepState(step_no=1, description="a", depends_on=[2]),
            MissionStepState(step_no=2, description="b", depends_on=[1]),
        ],
    )
    with pytest.raises(ValueError, match="invalid plan"):
        cyclic.waves()


def test_plan_waves_multi_wave_and_stable_wave_assignment():
    plan = MissionPlan(
        goal="g",
        steps=[
            MissionStepState(step_no=1, description="a"),
            MissionStepState(step_no=2, description="b"),
            MissionStepState(step_no=3, description="c", depends_on=[1]),
            MissionStepState(step_no=4, description="d", depends_on=[2, 3]),
        ],
    )
    waves = plan.waves()
    assert [[s.step_no for s in wave] for wave in waves] == [[1, 2], [3], [4]]
    assert [s.wave for s in plan.steps] == [0, 0, 1, 2]
    # Second call is stable (no drift in wave numbers).
    plan.waves()
    assert [s.wave for s in plan.steps] == [0, 0, 1, 2]


def test_continuable_reasons_frozen_set():
    assert {"max_turns", "token_budget_exceeded", "prompt_too_long"} == CONTINUABLE_SEGMENT_REASONS
    assert SegmentResult(reason="max_turns").continuable
    assert SegmentResult(reason="token_budget_exceeded").continuable
    assert SegmentResult(reason="prompt_too_long").continuable
    assert not SegmentResult(reason="completed").continuable
    assert not SegmentResult(reason="awaiting_user_input").continuable
