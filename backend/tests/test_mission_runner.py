"""Mission runner state-machine tests (in-memory store, no LLM)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest

from leagent.db.models.mission import (
    Mission,
    MissionAutonomy,
    MissionStatus,
    MissionStep,
    MissionStepStatus,
)
from leagent.mission import (
    MissionBudget,
    MissionPlan,
    MissionRunner,
    MissionStepState,
    MissionUsage,
    SegmentResult,
    StaticPlanner,
    VerificationResult,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from uuid import UUID


class FakeMissionStore:
    """In-memory :class:`MissionRepository` for state-machine tests."""

    def __init__(self) -> None:
        self.missions: dict[UUID, Mission] = {}
        self.steps: dict[UUID, list[MissionStep]] = {}

    async def get(self, mission_id: UUID) -> Mission | None:
        return self.missions.get(mission_id)

    async def get_with_steps(self, mission_id: UUID) -> tuple[Mission, list[MissionStep]] | None:
        mission = self.missions.get(mission_id)
        if mission is None:
            return None
        return mission, await self.list_steps(mission_id)

    async def create(self, mission: Mission) -> Mission:
        self.missions[mission.id] = mission
        return mission

    async def update(self, mission: Mission) -> Mission:
        self.missions[mission.id] = mission
        return mission

    async def update_status(
        self,
        mission_id: UUID,
        status: MissionStatus,
        *,
        pause_reason: str | None = None,
        error: str | None = None,
    ) -> bool:
        mission = self.missions.get(mission_id)
        if mission is None:
            return False
        mission.status = status
        if pause_reason is not None:
            mission.pause_reason = pause_reason
        if error is not None:
            mission.error = error
        return True

    async def list_for_user(
        self, user_id: UUID, *, status: MissionStatus | None = None, limit: int = 100
    ) -> list[Mission]:
        rows = [m for m in self.missions.values() if m.user_id == user_id]
        if status is not None:
            rows = [m for m in rows if m.status == status]
        return rows[:limit]

    async def delete(self, mission_id: UUID) -> None:
        self.missions.pop(mission_id, None)
        self.steps.pop(mission_id, None)

    async def list_steps(self, mission_id: UUID, *, plan_id: int | None = None) -> list[MissionStep]:
        rows = list(self.steps.get(mission_id, []))
        if plan_id is not None:
            rows = [s for s in rows if s.plan_id == plan_id]
        return sorted(rows, key=lambda s: s.step_no)

    async def get_step(self, mission_id: UUID, step_no: int, *, plan_id: int | None = None) -> MissionStep | None:
        for step in self.steps.get(mission_id, []):
            if step.step_no == step_no and (plan_id is None or step.plan_id == plan_id):
                return step
        return None

    async def upsert_step(self, step: MissionStep) -> MissionStep:
        rows = self.steps.setdefault(step.mission_id, [])
        for index, existing in enumerate(rows):
            if existing.plan_id == step.plan_id and existing.step_no == step.step_no:
                rows[index] = step
                return step
        rows.append(step)
        return step

    async def replace_steps(self, mission_id: UUID, plan_id: int, steps: list[MissionStep]) -> list[MissionStep]:
        kept = [s for s in self.steps.get(mission_id, []) if s.plan_id != plan_id]
        self.steps[mission_id] = kept + list(steps)
        return list(steps)


class ScriptedExecutor:
    """Returns results from a per-(step_no, attempt) script function."""

    def __init__(self, script: Callable[[int, int], SegmentResult]) -> None:
        self._script = script
        self.calls: list[tuple[int, int, str]] = []

    async def execute(self, *, mission, step, prompt, attempt):
        self.calls.append((step.step_no, attempt, prompt))
        return self._script(step.step_no, attempt)


class CountingVerifier:
    """Fails the first ``fail_times`` verifications per step, then passes."""

    def __init__(self, *, fail_times: int = 0, feedback: str = "不够好") -> None:
        self._fail_times = fail_times
        self._feedback = feedback
        self.verifications: dict[int, int] = {}

    async def verify(self, *, mission, step, result):
        step_no = step.step_no
        count = self.verifications.get(step_no, 0)
        self.verifications[step_no] = count + 1
        if count < self._fail_times:
            return VerificationResult(passed=False, feedback=self._feedback)
        return VerificationResult(passed=True)


class EventRecorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    async def sink(self, event: str, data: dict[str, Any]) -> None:
        self.events.append((event, data))


def make_mission(
    *,
    autonomy: MissionAutonomy = MissionAutonomy.FULL,
    budget_json: str | None = None,
) -> Mission:
    return Mission(goal="构建一个示例 CLI", autonomy=autonomy, budget_json=budget_json)


def linear_plan(*descriptions: str) -> MissionPlan:
    return MissionPlan(
        goal="构建一个示例 CLI",
        steps=[
            MissionStepState(
                step_no=index + 1,
                description=description,
                depends_on=[index] if index else [],
            )
            for index, description in enumerate(descriptions)
        ],
    )


def completed_script(step_no: int, attempt: int) -> SegmentResult:
    return SegmentResult(reason="completed", text=f"完成步骤 {step_no}")


async def test_run_completes_passing_steps():
    store = FakeMissionStore()
    mission = make_mission()
    await store.create(mission)
    events = EventRecorder()
    runner = MissionRunner(
        store=store,
        planner=StaticPlanner(linear_plan("写代码", "跑测试")),
        executor=ScriptedExecutor(completed_script),
        event_sink=events.sink,
    )
    result = await runner.run(mission)
    assert result.status == MissionStatus.COMPLETED
    steps = await store.list_steps(mission.id, plan_id=0)
    assert [s.status for s in steps] == [MissionStepStatus.PASSED] * 2
    assert [s.wave for s in steps] == [0, 1]
    names = [name for name, _ in events.events]
    assert names.count("mission_step_passed") == 2
    assert "mission_completed" in names


async def test_continuation_within_step_budget():
    store = FakeMissionStore()
    mission = make_mission()
    await store.create(mission)
    events = EventRecorder()

    def script(step_no: int, attempt: int) -> SegmentResult:
        if attempt == 0:
            return SegmentResult(reason="max_turns", text="部分完成")
        return SegmentResult(reason="completed", text="完成")

    runner = MissionRunner(
        store=store,
        planner=StaticPlanner(linear_plan("单步任务")),
        executor=ScriptedExecutor(script),
        event_sink=events.sink,
    )
    result = await runner.run(mission)
    assert result.status == MissionStatus.COMPLETED
    steps = await store.list_steps(mission.id, plan_id=0)
    assert steps[0].status == MissionStepStatus.PASSED
    assert steps[0].attempt == 2
    names = [name for name, _ in events.events]
    assert "mission_segment_continued" in names


async def test_segment_budget_exhausted_pauses_mission():
    store = FakeMissionStore()
    budget = MissionBudget(max_segments_per_step=1).to_json()
    mission = make_mission(budget_json=budget)
    await store.create(mission)
    runner = MissionRunner(
        store=store,
        planner=StaticPlanner(linear_plan("单步任务")),
        executor=ScriptedExecutor(lambda step_no, attempt: SegmentResult(reason="max_turns")),
    )
    result = await runner.run(mission)
    assert result.status == MissionStatus.PAUSED
    assert result.pause_reason == "step_failed"
    steps = await store.list_steps(mission.id, plan_id=0)
    assert steps[0].status == MissionStepStatus.FAILED
    assert steps[0].error == "segment budget exhausted"


async def test_verify_retry_then_pass():
    store = FakeMissionStore()
    mission = make_mission()
    await store.create(mission)
    runner = MissionRunner(
        store=store,
        planner=StaticPlanner(linear_plan("单步任务")),
        executor=ScriptedExecutor(completed_script),
        verifier=CountingVerifier(fail_times=1),
    )
    result = await runner.run(mission)
    assert result.status == MissionStatus.COMPLETED
    steps = await store.list_steps(mission.id, plan_id=0)
    assert steps[0].status == MissionStepStatus.PASSED
    # One initial segment + one verify-fix segment.
    assert steps[0].attempt == 2
    assert '"passed": true' in steps[0].verify_result


async def test_verify_retries_exhausted_fails_step():
    store = FakeMissionStore()
    budget = MissionBudget(max_verify_retries=0).to_json()
    mission = make_mission(budget_json=budget)
    await store.create(mission)
    runner = MissionRunner(
        store=store,
        planner=StaticPlanner(linear_plan("单步任务")),
        executor=ScriptedExecutor(completed_script),
        verifier=CountingVerifier(fail_times=99),
    )
    result = await runner.run(mission)
    assert result.status == MissionStatus.PAUSED
    steps = await store.list_steps(mission.id, plan_id=0)
    assert steps[0].status == MissionStepStatus.FAILED
    assert steps[0].error.startswith("verification failed:")


async def test_step_confirm_pauses_and_approves():
    store = FakeMissionStore()
    mission = make_mission(autonomy=MissionAutonomy.STEP_CONFIRM)
    await store.create(mission)
    runner = MissionRunner(
        store=store,
        planner=StaticPlanner(linear_plan("单步任务")),
        executor=ScriptedExecutor(completed_script),
    )
    result = await runner.run(mission)
    assert result.status == MissionStatus.PAUSED
    assert result.pause_reason == "step_confirm"
    steps = await store.list_steps(mission.id, plan_id=0)
    assert steps[0].status == MissionStepStatus.WAITING_APPROVAL

    approved = await runner.approve_step(mission, 1, approved=True)
    assert approved.status == MissionStatus.RUNNING
    steps = await store.list_steps(mission.id, plan_id=0)
    assert steps[0].status == MissionStepStatus.PASSED

    final = await runner.run(mission)
    assert final.status == MissionStatus.COMPLETED


async def test_step_failure_without_replanner_pauses():
    store = FakeMissionStore()
    mission = make_mission()
    await store.create(mission)

    def script(step_no: int, attempt: int) -> SegmentResult:
        if step_no == 2:
            return SegmentResult(reason="model_error", error="boom")
        return completed_script(step_no, attempt)

    runner = MissionRunner(
        store=store,
        planner=StaticPlanner(linear_plan("写代码", "跑测试")),
        executor=ScriptedExecutor(script),
    )
    result = await runner.run(mission)
    assert result.status == MissionStatus.PAUSED
    assert result.pause_reason == "step_failed"
    steps = await store.list_steps(mission.id, plan_id=0)
    assert steps[0].status == MissionStepStatus.PASSED
    assert steps[1].status == MissionStepStatus.FAILED


async def test_replan_after_step_failure():
    store = FakeMissionStore()
    mission = make_mission()
    await store.create(mission)
    replan = MissionPlan(
        goal="构建一个示例 CLI",
        steps=[MissionStepState(step_no=3, description="改用方案 B")],
    )

    def script(step_no: int, attempt: int) -> SegmentResult:
        if step_no == 2:
            return SegmentResult(reason="model_error", error="boom")
        return completed_script(step_no, attempt)

    runner = MissionRunner(
        store=store,
        planner=StaticPlanner(linear_plan("写代码", "跑测试"), replan=replan),
        executor=ScriptedExecutor(script),
    )
    result = await runner.run(mission)
    assert result.status == MissionStatus.COMPLETED
    assert mission.plan_id == 1
    old = await store.list_steps(mission.id, plan_id=0)
    new = await store.list_steps(mission.id, plan_id=1)
    assert old[1].status == MissionStepStatus.FAILED
    assert [s.status for s in new] == [MissionStepStatus.PASSED]


async def test_token_budget_pauses_mission():
    store = FakeMissionStore()
    budget = MissionBudget(max_total_tokens=10).to_json()
    mission = make_mission(budget_json=budget)
    await store.create(mission)
    runner = MissionRunner(
        store=store,
        planner=StaticPlanner(linear_plan("单步任务")),
        executor=ScriptedExecutor(lambda step_no, attempt: SegmentResult(usage={"total_tokens": 20})),
    )
    result = await runner.run(mission)
    assert result.status == MissionStatus.PAUSED
    assert result.pause_reason == "budget"


async def test_cost_budget_pauses_mission_and_accumulates_ledger():
    store = FakeMissionStore()
    budget = MissionBudget(max_cost_usd=1.0).to_json()
    mission = make_mission(budget_json=budget)
    await store.create(mission)
    runner = MissionRunner(
        store=store,
        planner=StaticPlanner(linear_plan("单步任务")),
        executor=ScriptedExecutor(
            lambda step_no, attempt: SegmentResult(text="烧钱", usage={"total_tokens": 10, "cost_usd": 1.25})
        ),
    )
    result = await runner.run(mission)
    assert result.status == MissionStatus.PAUSED
    assert result.pause_reason == "budget"
    usage = MissionUsage.from_json(mission.usage_json)
    assert usage.segments == 1
    assert usage.cost_usd == 1.25


async def test_continuation_folds_partial_text_into_summary_and_prompt():
    store = FakeMissionStore()
    mission = make_mission()
    await store.create(mission)

    def script(step_no: int, attempt: int) -> SegmentResult:
        if attempt == 0:
            return SegmentResult(reason="max_turns", text="已完成一半", checkpoint_id="cp-1")
        return SegmentResult(reason="completed", text="全部完成")

    executor = ScriptedExecutor(script)
    runner = MissionRunner(
        store=store,
        planner=StaticPlanner(linear_plan("单步任务")),
        executor=executor,
    )
    result = await runner.run(mission)
    assert result.status == MissionStatus.COMPLETED
    steps = await store.list_steps(mission.id, plan_id=0)
    assert steps[0].status == MissionStepStatus.PASSED
    assert steps[0].checkpoint_id == "cp-1"
    # The continuation prompt carries the exhausted segment's partial text.
    continuation_prompt = executor.calls[1][2]
    assert "已有进展" in continuation_prompt
    assert "已完成一半" in continuation_prompt


async def test_deadline_expires_mission():
    store = FakeMissionStore()
    past = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=1)
    budget = MissionBudget(deadline=past).to_json()
    mission = make_mission(budget_json=budget)
    await store.create(mission)
    runner = MissionRunner(
        store=store,
        planner=StaticPlanner(linear_plan("单步任务")),
        executor=ScriptedExecutor(completed_script),
    )
    result = await runner.run(mission)
    assert result.status == MissionStatus.EXPIRED
    assert result.pause_reason == "deadline"


async def test_run_recovers_stuck_running_step():
    store = FakeMissionStore()
    mission = make_mission()
    await store.create(mission)
    runner = MissionRunner(
        store=store,
        planner=StaticPlanner(linear_plan("单步任务")),
        executor=ScriptedExecutor(completed_script),
    )
    await runner.start(mission)
    # Simulate a crash mid-segment: the step is left in RUNNING.
    step = await store.get_step(mission.id, 1, plan_id=0)
    step.status = MissionStepStatus.RUNNING
    await store.upsert_step(step)

    result = await runner.run(mission)
    assert result.status == MissionStatus.COMPLETED
    steps = await store.list_steps(mission.id, plan_id=0)
    assert steps[0].status == MissionStepStatus.PASSED


async def test_invalid_plan_fails_mission():
    store = FakeMissionStore()
    mission = make_mission()
    await store.create(mission)
    cyclic = MissionPlan(
        goal="构建一个示例 CLI",
        steps=[
            MissionStepState(step_no=1, description="a", depends_on=[2]),
            MissionStepState(step_no=2, description="b", depends_on=[1]),
        ],
    )
    runner = MissionRunner(
        store=store,
        planner=StaticPlanner(cyclic),
        executor=ScriptedExecutor(completed_script),
    )
    result = await runner.run(mission)
    assert result.status == MissionStatus.FAILED
    assert "dependency cycle" in result.error


async def test_abort_is_terminal():
    store = FakeMissionStore()
    mission = make_mission()
    await store.create(mission)
    runner = MissionRunner(
        store=store,
        planner=StaticPlanner(linear_plan("单步任务")),
        executor=ScriptedExecutor(completed_script),
    )
    result = await runner.abort(mission)
    assert result.status == MissionStatus.ABORTED
    assert result.completed_at is not None


async def test_pause_and_resume_transitions():
    store = FakeMissionStore()
    mission = make_mission()
    await store.create(mission)
    runner = MissionRunner(
        store=store,
        planner=StaticPlanner(linear_plan("单步任务")),
        executor=ScriptedExecutor(completed_script),
    )
    await runner.start(mission)
    paused = await runner.pause(mission)
    assert paused.status == MissionStatus.PAUSED
    assert paused.pause_reason == "user"
    resumed = await runner.resume(mission)
    assert resumed.status == MissionStatus.RUNNING
    with pytest.raises(ValueError):
        await runner.resume(mission)


def test_plan_waves_group_by_dependencies():
    plan = MissionPlan(
        goal="g",
        steps=[
            MissionStepState(step_no=1, description="a"),
            MissionStepState(step_no=2, description="b"),
            MissionStepState(step_no=3, description="c", depends_on=[1, 2]),
        ],
    )
    waves = plan.waves()
    assert [[s.step_no for s in wave] for wave in waves] == [[1, 2], [3]]
    assert [s.wave for s in plan.steps] == [0, 0, 1]


def test_plan_validate_unknown_dependency_and_cap():
    plan = MissionPlan(
        goal="g",
        steps=[MissionStepState(step_no=1, description="a", depends_on=[9])],
    )
    errors = plan.validate(max_steps=20)
    assert any("unknown step 9" in error for error in errors)
    capped = MissionPlan(
        goal="g",
        steps=[MissionStepState(step_no=index + 1, description="s") for index in range(3)],
    )
    errors = capped.validate(max_steps=2)
    assert any("max_steps=2" in error for error in errors)
