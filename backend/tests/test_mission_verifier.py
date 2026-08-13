"""Tests for :class:`HeuristicVerifier` (pure heuristic gate, no LLM)."""

from __future__ import annotations

from typing import Any

from leagent.db.models.mission import (
    Mission,
    MissionStep,
    MissionStepKind,
    MissionVerifyLevel,
)
from leagent.mission.types import SegmentResult
from leagent.mission.verifier import HeuristicVerifier


def make_mission(**kwargs: Any) -> Mission:
    return Mission(goal="修复 bug", **kwargs)


def make_step(**kwargs: Any) -> MissionStep:
    defaults: dict[str, Any] = {
        "mission_id": make_mission().id,
        "plan_id": 0,
        "step_no": 1,
        "description": "修复 bug",
        "kind": MissionStepKind.GENERIC,
    }
    defaults.update(kwargs)
    return MissionStep(**defaults)


def code_step() -> MissionStep:
    return make_step(kind=MissionStepKind.CODE)


async def test_code_step_with_unverified_changes_fails():
    verifier = HeuristicVerifier()
    verdict = await verifier.verify(
        mission=make_mission(),
        step=code_step(),
        result=SegmentResult(reason="completed", text="改了代码", changed_paths=["src/app.py"]),
    )
    assert not verdict.passed
    assert "project_shell was not run" in verdict.feedback
    assert verdict.evidence["verification_gap"]


async def test_code_step_with_matching_test_command_passes():
    verifier = HeuristicVerifier()
    verdict = await verifier.verify(
        mission=make_mission(),
        step=code_step(),
        result=SegmentResult(
            reason="completed",
            text="修好了",
            changed_paths=["src/app.py"],
            shell_blobs=["pytest tests/ -q"],
        ),
    )
    assert verdict.passed


async def test_code_step_without_changed_files_passes():
    verifier = HeuristicVerifier()
    verdict = await verifier.verify(
        mission=make_mission(),
        step=code_step(),
        result=SegmentResult(reason="completed", text="纯推理步骤"),
    )
    assert verdict.passed


async def test_code_verify_can_be_relaxed():
    verifier = HeuristicVerifier(enforce_code_verify=False)
    verdict = await verifier.verify(
        mission=make_mission(),
        step=code_step(),
        result=SegmentResult(reason="completed", text="改了代码", changed_paths=["src/app.py"]),
    )
    assert verdict.passed


async def test_file_step_requires_artifacts():
    verifier = HeuristicVerifier()
    step = make_step(kind=MissionStepKind.FILE)
    failed = await verifier.verify(
        mission=make_mission(),
        step=step,
        result=SegmentResult(reason="completed", text="做完了"),
    )
    assert not failed.passed
    assert "未产出任何文件" in failed.feedback
    passed = await verifier.verify(
        mission=make_mission(),
        step=step,
        result=SegmentResult(reason="completed", text="做完了", produced_files=["out/x.pdf"]),
    )
    assert passed.passed


async def test_empty_segment_text_fails():
    verifier = HeuristicVerifier()
    verdict = await verifier.verify(
        mission=make_mission(),
        step=make_step(),
        result=SegmentResult(reason="completed"),
    )
    assert not verdict.passed
    assert "未产生任何文本输出" in verdict.feedback


async def test_verify_level_none_always_passes():
    verifier = HeuristicVerifier()
    verdict = await verifier.verify(
        mission=make_mission(verify_level=MissionVerifyLevel.NONE),
        step=code_step(),
        result=SegmentResult(reason="completed", changed_paths=["src/app.py"]),
    )
    assert verdict.passed
    assert verdict.evidence["verify_level"] == "none"
