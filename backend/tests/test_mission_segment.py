"""Tests for :class:`KernelSegmentExecutor` (injected fake runtime, no LLM)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from leagent.db.models.mission import Mission, MissionAutonomy, MissionStep
from leagent.mission.segment import KernelSegmentExecutor
from leagent.sdk.events import AgentEvent


class FakeRuntime:
    """Stub ``AgentRuntime`` recording the definition and streaming events."""

    def __init__(self, events: list[AgentEvent]) -> None:
        self._events = events
        self.definition: Any = None
        self.calls: list[dict[str, Any]] = []

    async def stream(self, definition: Any, prompt: str, **kwargs: Any):
        self.definition = definition
        self.calls.append({"prompt": prompt, **kwargs})
        for event in self._events:
            yield event


def make_mission(**kwargs: Any) -> Mission:
    defaults: dict[str, Any] = {
        "goal": "修复一个 bug",
        "agent_id": "coding_agent",
        "autonomy": MissionAutonomy.FULL,
    }
    defaults.update(kwargs)
    return Mission(**defaults)


def make_step() -> MissionStep:
    return MissionStep(mission_id=make_mission().id, plan_id=0, step_no=1, description="修复 bug")


async def test_collects_transcript_and_terminal_reason():
    events = [
        AgentEvent(type="stream_delta", data={"content": "正在"}),
        AgentEvent(type="stream_delta", data={"content": "修复…"}),
        AgentEvent(type="assistant", data={"content": "修复完成"}),
        AgentEvent(type="result", data={"reason": "completed", "usage": {"total_tokens": 42}}),
    ]
    executor = KernelSegmentExecutor(runtime=FakeRuntime(events))
    result = await executor.execute(
        mission=make_mission(),
        step=make_step(),
        prompt="修 bug",
        attempt=0,
    )
    assert result.reason == "completed"
    assert result.text == "修复完成"
    assert result.tokens_used == 42
    assert result.succeeded


async def test_collects_tool_activity_for_verifier():
    events = [
        AgentEvent(
            type="tool_use",
            data={"name": "project_write", "input": {"path": "src/app.py"}},
        ),
        AgentEvent(
            type="tool_use",
            data={"name": "project_shell", "input": {"argv": ["pytest", "tests/"]}},
        ),
        AgentEvent(type="tool_result", data={"name": "project_shell", "content": "ok"}),
        AgentEvent(type="assistant", data={"content": "完成"}),
        AgentEvent(type="result", data={"reason": "completed"}),
    ]
    executor = KernelSegmentExecutor(runtime=FakeRuntime(events))
    result = await executor.execute(
        mission=make_mission(),
        step=make_step(),
        prompt="修 bug",
        attempt=0,
    )
    assert result.changed_paths == ["src/app.py"]
    assert "pytest tests/" in result.shell_blobs
    assert [row["tool"] for row in result.activity] == ["project_write", "project_shell"]


async def test_maps_terminal_reason_and_checkpoint():
    events = [
        AgentEvent(type="assistant", data={"content": "需要澄清"}),
        AgentEvent(
            type="result",
            data={"reason": "awaiting_user_input", "checkpoint_id": "cp-1", "error": None},
        ),
    ]
    executor = KernelSegmentExecutor(runtime=FakeRuntime(events))
    result = await executor.execute(
        mission=make_mission(),
        step=make_step(),
        prompt="修 bug",
        attempt=0,
    )
    assert result.reason == "awaiting_user_input"
    assert result.checkpoint_id == "cp-1"
    assert not result.succeeded


async def test_collects_produced_files():
    events = [
        AgentEvent(type="workspace_attachments", data={"paths": ["out/report.md"]}),
        AgentEvent(type="assistant", data={"content": "已生成报告"}),
        AgentEvent(type="result", data={"reason": "completed"}),
    ]
    executor = KernelSegmentExecutor(runtime=FakeRuntime(events))
    result = await executor.execute(
        mission=make_mission(),
        step=make_step(),
        prompt="生成报告",
        attempt=0,
    )
    assert result.produced_files == ["out/report.md"]


async def test_stream_error_becomes_model_error_result():
    class BrokenRuntime(FakeRuntime):
        async def stream(self, definition: Any, prompt: str, **kwargs: Any):
            self.definition = definition
            raise RuntimeError("boom")
            yield  # pragma: no cover - make this an async generator

    executor = KernelSegmentExecutor(runtime=BrokenRuntime([]))
    result = await executor.execute(
        mission=make_mission(),
        step=make_step(),
        prompt="修 bug",
        attempt=0,
    )
    assert result.reason == "model_error"
    assert "boom" in (result.error or "")


async def test_readonly_autonomy_scopes_tool_allowlist(monkeypatch):
    from leagent.tools import registry as registry_module

    class FakeRegistry:
        def get_enabled_tools(self) -> list[SimpleNamespace]:
            return [
                SimpleNamespace(name="web_search", is_read_only=True),
                SimpleNamespace(name="project_write", is_read_only=False),
            ]

    monkeypatch.setattr(registry_module, "get_registry", lambda: FakeRegistry())
    events = [
        AgentEvent(type="assistant", data={"content": "完成"}),
        AgentEvent(type="result", data={"reason": "completed"}),
    ]
    runtime = FakeRuntime(events)
    executor = KernelSegmentExecutor(runtime=runtime)
    await executor.execute(
        mission=make_mission(autonomy=MissionAutonomy.READONLY),
        step=make_step(),
        prompt="调研",
        attempt=0,
    )
    assert runtime.definition.tools.allow == ["web_search"]


async def test_full_autonomy_has_no_allowlist():
    events = [
        AgentEvent(type="assistant", data={"content": "完成"}),
        AgentEvent(type="result", data={"reason": "completed"}),
    ]
    runtime = FakeRuntime(events)
    executor = KernelSegmentExecutor(runtime=runtime)
    await executor.execute(
        mission=make_mission(),
        step=make_step(),
        prompt="修 bug",
        attempt=0,
    )
    assert runtime.definition.tools.allow == []


async def test_definition_uses_mission_agent_and_memory_disabled():
    events = [
        AgentEvent(type="assistant", data={"content": "完成"}),
        AgentEvent(type="result", data={"reason": "completed"}),
    ]
    runtime = FakeRuntime(events)
    executor = KernelSegmentExecutor(runtime=runtime, agent_id="fallback_agent")
    await executor.execute(
        mission=make_mission(),
        step=make_step(),
        prompt="修 bug",
        attempt=0,
    )
    assert runtime.definition.name == "coding_agent"
    assert runtime.definition.prompt_variant == "coding_agent"
    assert runtime.definition.memory.enabled is False
    assert runtime.calls[0]["tool_extra"]["step_no"] == 1


async def test_tool_extra_carries_mission_step_attempt_context():
    events = [
        AgentEvent(type="assistant", data={"content": "完成"}),
        AgentEvent(type="result", data={"reason": "completed"}),
    ]
    runtime = FakeRuntime(events)
    executor = KernelSegmentExecutor(runtime=runtime)
    mission = make_mission()
    await executor.execute(
        mission=mission,
        step=make_step(),
        prompt="修 bug",
        attempt=2,
    )
    tool_extra = runtime.calls[0]["tool_extra"]
    assert tool_extra["mission_id"] == str(mission.id)
    assert tool_extra["step_no"] == 1
    assert tool_extra["attempt"] == 2
