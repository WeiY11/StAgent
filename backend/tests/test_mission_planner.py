"""Tests for :class:`LLMMissionPlanner` (fake LLM, no network)."""

from __future__ import annotations

from typing import Any

from leagent.db.models.mission import Mission, MissionStep, MissionStepStatus
from leagent.mission.planner import LLMMissionPlanner
from leagent.mission.types import MissionStepState


class FakeLLM:
    """Returns canned chat responses in order, recording each call."""

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
        self.calls.append({"messages": messages, **kwargs})
        return {"content": self._responses.pop(0)}


class FakeStore:
    """Mission repository exposing only ``list_steps`` (replan)."""

    def __init__(self, steps: list[MissionStep]) -> None:
        self._steps = steps

    async def list_steps(self, mission_id: Any, *, plan_id: int | None = None) -> list[MissionStep]:
        return [s for s in self._steps if plan_id is None or s.plan_id == plan_id]


def make_mission(**kwargs: Any) -> Mission:
    return Mission(goal="构建一个 CLI", **kwargs)


PLAN_JSON = """```json
{
  "goal": "构建一个 CLI",
  "steps": [
    {"id": 2, "description": "编写核心代码", "depends_on": []},
    {"id": 7, "description": "运行测试", "depends_on": [2]},
    {"id": 9, "description": "撰写 README 文档", "depends_on": [7]}
  ],
  "expected_output": "可运行的 CLI"
}
```"""


async def test_plan_parses_fences_and_renumbers_steps():
    planner = LLMMissionPlanner(FakeLLM([PLAN_JSON]))
    plan = await planner.plan(make_mission())
    assert plan.goal == "构建一个 CLI"
    assert plan.expected_output == "可运行的 CLI"
    assert [s.step_no for s in plan.steps] == [1, 2, 3]
    assert plan.steps[0].description == "编写核心代码"
    assert plan.steps[1].depends_on == [1]
    assert plan.steps[2].depends_on == [2]
    # Kind inference: code step, generic test step, file step.
    assert [s.kind for s in plan.steps] == ["code", "code", "file"]
    # The shared planner template is used (goal + step schema are injected).
    content = planner._llm.calls[0]["messages"][0]["content"]
    assert "you are a task planner" in content.lower()
    assert "构建一个 CLI" in content


async def test_plan_parse_failure_yields_empty_steps():
    planner = LLMMissionPlanner(FakeLLM(["not json at all"]))
    plan = await planner.plan(make_mission())
    assert plan.steps == []


async def test_replan_keeps_only_remaining_steps():
    mission = make_mission()
    rows = [
        MissionStep(
            mission_id=mission.id,
            plan_id=0,
            step_no=1,
            description="写代码",
            status=MissionStepStatus.PASSED,
            segment_summary="完成",
        ),
        MissionStep(
            mission_id=mission.id,
            plan_id=0,
            step_no=2,
            description="跑测试",
            status=MissionStepStatus.FAILED,
            error="pytest boom",
        ),
    ]
    response = """{
      "goal": "构建一个 CLI",
      "steps": [
        {"id": 1, "description": "重复的已完成步骤", "depends_on": []},
        {"id": 5, "description": "改用方案 B", "depends_on": [1, 2]}
      ]
    }"""
    planner = LLMMissionPlanner(FakeLLM([response]), store=FakeStore(rows))
    plan = await planner.replan(mission, rows[1])
    assert plan is not None
    # Step 1 echoes a completed id → dropped; step 5 survives and is renumbered.
    assert [s.step_no for s in plan.steps] == [1]
    assert plan.steps[0].description == "改用方案 B"
    assert plan.steps[0].depends_on == []  # references to completed ids stripped
    content = planner._llm.calls[0]["messages"][0]["content"]
    assert "- ID: 2" in content and "pytest boom" in content


async def test_replan_empty_response_returns_none():
    mission = make_mission()
    planner = LLMMissionPlanner(FakeLLM(["{}\n"]), store=FakeStore([]))
    failed = MissionStep(
        mission_id=mission.id,
        plan_id=0,
        step_no=2,
        description="跑测试",
        status=MissionStepStatus.FAILED,
    )
    assert await planner.replan(mission, failed) is None


async def test_replan_without_store_degrades():
    mission = make_mission()
    response = '{"steps": [{"id": 3, "description": "替代步骤"}]}'
    planner = LLMMissionPlanner(FakeLLM([response]))
    failed = MissionStep(
        mission_id=mission.id,
        plan_id=0,
        step_no=2,
        description="跑测试",
        status=MissionStepStatus.FAILED,
    )
    plan = await planner.replan(mission, failed)
    assert plan is not None
    assert [s.step_no for s in plan.steps] == [1]


def test_kind_inference_keywords():
    from leagent.mission.planner import _infer_kind

    assert _infer_kind("进行市场调研", "") == "research"
    assert _infer_kind("分析销售数据", "") == "data"
    assert _infer_kind("导出最终报告", "") == "file"
    assert _infer_kind("随便做点什么", "") == "generic"
    assert _infer_kind("", "code_execution") == "code"


def test_renumber_remaps_dependencies():
    from leagent.mission.planner import _renumber_steps

    steps = [
        MissionStepState(step_no=5, description="a"),
        MissionStepState(step_no=9, description="b", depends_on=[5, 99]),
    ]
    _renumber_steps(steps)
    assert [s.step_no for s in steps] == [1, 2]
    assert steps[1].depends_on == [1]
