"""Mission autonomy-loop integration tests (SQLite store, scripted runtime).

End-to-end coverage of the mission loop over the real
:class:`~leagent.db.repositories.missions.DbMissionRepository` with a
scripted segment runtime (no LLM, no network): multi-step wave execution,
bounded continuation on budget exhaustion, the ``readonly`` autonomy tool
whitelist, and the verify-fail → invalid-replan → pause path.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest_asyncio

from leagent.config.settings import Settings
from leagent.db.models.mission import (
    Mission,
    MissionAutonomy,
    MissionStatus,
    MissionStep,
    MissionStepStatus,
)
from leagent.db.service import DatabaseService
from leagent.mission import (
    MissionPlan,
    MissionRunner,
    MissionStepState,
    MissionUsage,
    SegmentResult,
    StaticPlanner,
    VerificationResult,
)
from leagent.mission.segment import KernelSegmentExecutor
from leagent.sdk.events import AgentEvent

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


class ScriptedRuntime:
    """Stub ``AgentRuntime`` replaying events per (step_no, attempt)."""

    def __init__(self, script: Any) -> None:
        self._script = script
        self.definitions: list[Any] = []
        self.stream_calls: list[dict[str, Any]] = []

    async def stream(self, definition: Any, prompt: str, **kwargs: Any):
        self.definitions.append(definition)
        self.stream_calls.append({"prompt": prompt, **kwargs})
        tool_extra = kwargs.get("tool_extra") or {}
        for event in self._script(int(tool_extra.get("step_no", 0)), int(tool_extra.get("attempt", 0))):
            yield event


def completed(text: str) -> list[AgentEvent]:
    return [
        AgentEvent(type="assistant", data={"content": text}),
        AgentEvent(type="result", data={"reason": "completed", "usage": {"total_tokens": 10}}),
    ]


def max_turns(text: str) -> list[AgentEvent]:
    return [
        AgentEvent(type="assistant", data={"content": text}),
        AgentEvent(type="result", data={"reason": "max_turns", "usage": {"total_tokens": 10}}),
    ]


def three_step_plan() -> MissionPlan:
    return MissionPlan(
        goal="构建一个 CLI",
        expected_output="可运行的 CLI",
        steps=[
            MissionStepState(step_no=1, description="写代码", kind="code"),
            MissionStepState(step_no=2, description="写测试", kind="code"),
            MissionStepState(step_no=3, description="汇总验证", kind="generic", depends_on=[1, 2]),
        ],
    )


class ScriptedVerifier:
    """Fails verification for a given set of (step_no, attempt) pairs."""

    def __init__(self, fail: set[tuple[int, int]] | None = None, feedback: str = "未达标") -> None:
        self._fail = fail or set()
        self._feedback = feedback
        self.verifications: list[tuple[int, int]] = []

    async def verify(self, *, mission: Mission, step: MissionStep, result: SegmentResult) -> VerificationResult:
        key = (step.step_no, step.attempt)
        self.verifications.append(key)
        if key in self._fail:
            return VerificationResult(passed=False, feedback=self._feedback)
        return VerificationResult(passed=True)


class EventRecorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    async def sink(self, event: str, data: dict[str, Any]) -> None:
        self.events.append((event, data))


@pytest_asyncio.fixture()
async def repo_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    db_path = tmp_path / "mission_integration.db"
    monkeypatch.setenv("DB_DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    db = DatabaseService(Settings())
    await db.create_tables()
    try:
        yield db
    finally:
        await db.dispose()


async def _run_mission(
    repo_db: DatabaseService,
    *,
    runtime: ScriptedRuntime,
    plan: MissionPlan,
    verifier: Any = None,
    autonomy: MissionAutonomy = MissionAutonomy.FULL,
    replan: MissionPlan | None = None,
) -> tuple[Mission, list[MissionStep], ScriptedRuntime, EventRecorder]:
    mission = await repo_db.repositories.missions.create(Mission(goal=plan.goal, autonomy=autonomy))
    events = EventRecorder()
    runner = MissionRunner(
        store=repo_db.repositories.missions,
        planner=StaticPlanner(plan, replan=replan),
        executor=KernelSegmentExecutor(runtime=runtime),
        verifier=verifier,
        event_sink=events.sink,
    )
    result = await runner.run(mission)
    steps = await repo_db.repositories.missions.list_steps(result.id, plan_id=result.plan_id)
    return result, steps, runtime, events


async def test_three_step_mission_with_continuation_completes(repo_db):
    def script(step_no: int, attempt: int) -> list[AgentEvent]:
        if step_no == 2 and attempt == 0:
            return max_turns("步骤2完成一半")
        return completed(f"完成步骤 {step_no}")

    runtime = ScriptedRuntime(script)
    mission, steps, _runtime, events = await _run_mission(repo_db, runtime=runtime, plan=three_step_plan())

    assert mission.status == MissionStatus.COMPLETED
    assert [s.status for s in steps] == [MissionStepStatus.PASSED] * 3
    # Waves persisted at plan time: steps 1+2 in wave 0, step 3 in wave 1.
    assert [s.wave for s in steps] == [0, 0, 1]
    # Step 2 consumed two segments: max_turns continuation then completion.
    assert steps[1].attempt == 2
    assert steps[1].segment_summary == "完成步骤 2"
    names = [name for name, _ in events.events]
    assert "mission_segment_continued" in names
    assert names.count("mission_step_passed") == 3
    assert names[-1] == "mission_completed"
    # The continuation prompt carried the exhausted segment's partial text.
    continuation_prompts = [
        call["prompt"] for call in runtime.stream_calls if call.get("tool_extra", {}).get("attempt") == 1
    ]
    assert any("步骤2完成一半" in prompt for prompt in continuation_prompts)
    # The usage ledger accumulated every segment.
    usage = MissionUsage.from_json(mission.usage_json)
    assert usage.segments == 4


async def test_readonly_mission_scopes_tool_allowlist(repo_db, monkeypatch):
    from leagent.tools import registry as registry_module

    class FakeRegistry:
        def get_enabled_tools(self) -> list[SimpleNamespace]:
            return [
                SimpleNamespace(name="web_search", is_read_only=True),
                SimpleNamespace(name="doc_read", is_read_only=True),
                SimpleNamespace(name="project_write", is_read_only=False),
                SimpleNamespace(name="project_shell", is_read_only=False),
            ]

    monkeypatch.setattr(registry_module, "get_registry", lambda: FakeRegistry())
    runtime = ScriptedRuntime(lambda step_no, attempt: completed("调研完成"))
    mission, steps, _runtime, _events = await _run_mission(
        repo_db,
        runtime=runtime,
        plan=MissionPlan(
            goal="调研",
            steps=[MissionStepState(step_no=1, description="查资料", kind="research")],
        ),
        autonomy=MissionAutonomy.READONLY,
    )

    assert mission.status == MissionStatus.COMPLETED
    assert steps[0].status == MissionStepStatus.PASSED
    assert runtime.definitions[0].tools.allow == ["web_search", "doc_read"]


async def test_verify_fail_with_invalid_replan_pauses(repo_db):
    plan = MissionPlan(
        goal="构建一个 CLI",
        steps=[
            MissionStepState(step_no=1, description="写代码", kind="code"),
            MissionStepState(step_no=2, description="跑测试", kind="code", depends_on=[1]),
        ],
    )
    # Replan echoes a cyclic graph → the runner must reject it and pause.
    replan = MissionPlan(
        goal="构建一个 CLI",
        steps=[
            MissionStepState(step_no=1, description="a", depends_on=[2]),
            MissionStepState(step_no=2, description="b", depends_on=[1]),
        ],
    )
    runtime = ScriptedRuntime(lambda step_no, attempt: completed(f"完成步骤 {step_no}"))
    # Default budget (max_verify_retries=2): three failed verifications
    # exhaust the retry cap and fail the step.
    verifier = ScriptedVerifier(fail={(2, 1), (2, 2), (2, 3)})
    mission, steps, _runtime, events = await _run_mission(
        repo_db,
        runtime=runtime,
        plan=plan,
        verifier=verifier,
        replan=replan,
    )

    assert mission.status == MissionStatus.PAUSED
    assert mission.pause_reason == "step_failed"
    assert [s.status for s in steps] == [MissionStepStatus.PASSED, MissionStepStatus.FAILED]
    names = [name for name, _ in events.events]
    assert "mission_step_verify_failed" in names
    assert "mission_replanned" not in names
