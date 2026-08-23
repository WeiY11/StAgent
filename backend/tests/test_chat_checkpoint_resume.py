"""Chat abort → durable checkpoint → cancel/resume contract tests."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from fastapi import HTTPException

from StAgent.agent.base import AgentConfig, AgentContext, ConversationContext, NoOpStreamHandler, StreamEvent
from StAgent.agent.controller import AgentController
from StAgent.agent.query_engine import SDKMessage
from StAgent.agent.transitions import TerminalReason
from StAgent.api.schemas.chat import ResumeCheckpointRequest, SessionCancelResponse
from StAgent.api.v1 import chat as chat_api
from StAgent.api.v1.chat.agent_stream import run_agent_stream
from StAgent.sdk.kernel.checkpoint import InMemoryCheckpointStore, create_checkpoint
from StAgent.sdk.kernel.loop import RESUMABLE_CHECKPOINT_REASONS, run_loop
from StAgent.sdk.kernel.state import RunState


class _FakeConfig:
    session_id = None
    user_id = None
    agent_id = "test_agent"


class _FakeEngine:
    def __init__(self, scripted: list[SDKMessage]) -> None:
        self._scripted = scripted
        self.mutable_messages: list[dict] = []
        self.abort_event = asyncio.Event()
        self.config = _FakeConfig()

    async def submit_message(self, prompt, **kwargs):
        self.mutable_messages.append({"role": "user", "content": str(prompt)})
        for msg in self._scripted:
            if msg.type == "assistant":
                self.mutable_messages.append({"role": "assistant", "content": msg.data.get("content", "")})
            yield msg


@pytest.mark.asyncio
async def test_resumable_reasons_include_abort_and_limits() -> None:
    assert "aborted_streaming" in RESUMABLE_CHECKPOINT_REASONS
    assert "max_turns" in RESUMABLE_CHECKPOINT_REASONS
    assert "completed" not in RESUMABLE_CHECKPOINT_REASONS


@pytest.mark.asyncio
async def test_stash_and_wait_session_checkpoint() -> None:
    sid = uuid4()
    AgentController._session_last_checkpoint.pop(sid, None)
    AgentController._session_checkpoint_events[sid] = asyncio.Event()

    async def _stash_later() -> None:
        await asyncio.sleep(0.05)
        AgentController.stash_session_checkpoint(sid, "cp-wait-1")

    task = asyncio.create_task(_stash_later())
    cpid = await AgentController.wait_session_checkpoint(sid, timeout=1.0)
    await task
    assert cpid == "cp-wait-1"
    assert AgentController.peek_session_checkpoint(sid) == "cp-wait-1"


@pytest.mark.asyncio
async def test_checkpoint_aborted_turn_persists_and_stashes() -> None:
    """Controller abort helper writes a durable checkpoint and stashes the id."""
    from StAgent.runtime import AgentRuntime, RuntimeContext

    store = InMemoryCheckpointStore()
    sid = uuid4()
    AgentController._session_checkpoint_events[sid] = asyncio.Event()
    AgentController._session_last_checkpoint.pop(sid, None)

    # Minimal controller shell — only needs runtime + checkpoint helpers.
    ctrl = object.__new__(AgentController)
    ctrl._runtime = AgentRuntime(RuntimeContext(checkpoint_store=store))
    ctrl._last_checkpoint_id = None

    engine = _FakeEngine([])
    engine.mutable_messages = [
        {"role": "user", "content": "do work"},
        {"role": "assistant", "content": "working…"},
    ]
    run_state = RunState(session_id=str(sid), agent_name="default_agent", turn=2)

    cpid = await ctrl._checkpoint_aborted_turn(
        session_id=sid,
        engine=engine,
        run_state=run_state,
        agent_name="default_agent",
    )
    assert cpid is not None
    saved = await store.load(cpid)
    assert saved is not None
    assert saved.reason == TerminalReason.ABORTED_STREAMING.value
    assert saved.messages == engine.mutable_messages
    assert AgentController.peek_session_checkpoint(sid) == cpid


@pytest.mark.asyncio
async def test_controller_rejects_checkpoint_from_another_session() -> None:
    """A checkpoint id must never import a different session's transcript."""
    from StAgent.runtime import AgentRuntime, RuntimeContext

    owner_session_id = uuid4()
    requested_session_id = uuid4()
    store = InMemoryCheckpointStore()
    foreign_checkpoint = create_checkpoint(
        session_id=str(owner_session_id),
        agent_name="default_agent",
        turn=1,
        messages=[{"role": "user", "content": "owner-only context"}],
    )
    await store.save(foreign_checkpoint)

    ctrl = object.__new__(AgentController)
    ctrl._runtime = AgentRuntime(RuntimeContext(checkpoint_store=store))
    ctrl.config = AgentConfig()
    ctrl.session_manager = None

    with pytest.raises(ValueError, match="does not belong to session"):
        await ctrl._run_via_query_engine(
            "Continue",
            ConversationContext(session_id=requested_session_id),
            AgentContext(session_id=requested_session_id),
            NoOpStreamHandler(),
            checkpoint_id=foreign_checkpoint.checkpoint_id,
        )


@pytest.mark.asyncio
async def test_resume_endpoint_rejects_checkpoint_from_another_session(monkeypatch) -> None:
    """The HTTP handshake must not acknowledge a foreign session checkpoint."""
    owner_session_id = uuid4()
    requested_session_id = uuid4()
    store = InMemoryCheckpointStore()
    foreign_checkpoint = create_checkpoint(
        session_id=str(owner_session_id),
        agent_name="default_agent",
        turn=1,
    )
    await store.save(foreign_checkpoint)

    class _ChatService:
        async def get_session(self, _session_id, *, user_id=None):  # noqa: ARG002
            return SimpleNamespace(id=requested_session_id)

    async def _allow_project_access(*args, **kwargs):  # noqa: ANN002, ANN003
        return None

    monkeypatch.setattr(
        "StAgent.services.service_manager.get_service_manager",
        lambda: SimpleNamespace(database_service=object()),
    )
    monkeypatch.setattr(
        "StAgent.sdk.kernel.checkpoint.build_checkpoint_store",
        lambda _database: store,
    )
    monkeypatch.setattr(chat_api, "_require_session_project_access", _allow_project_access)

    with pytest.raises(HTTPException) as exc_info:
        await chat_api.resume_checkpoint(
            requested_session_id,
            ResumeCheckpointRequest(checkpoint_id=foreign_checkpoint.checkpoint_id),
            uuid4(),
            _ChatService(),
            object(),
        )

    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_run_agent_stream_forwards_checkpoint_id() -> None:
    captured: dict[str, Any] = {}

    class _CapturingAgent:
        async def run_stream(self, *args, **kwargs):
            captured.update(kwargs)
            yield StreamEvent(type="complete", data={"text": "ok", "terminal_reason": "completed"})

    session_id = uuid4()
    user_id = uuid4()
    events = [
        (etype, data, acc)
        async for (etype, data, acc) in run_agent_stream(
            _CapturingAgent(),
            "Continue",
            session_id,
            user_id,
            checkpoint_id="cp-resume-42",
        )
    ]
    assert captured.get("checkpoint_id") == "cp-resume-42"
    assert any(etype == "complete" for etype, _, _ in events)


def test_session_cancel_response_includes_optional_checkpoint() -> None:
    body = SessionCancelResponse(
        session_id=str(uuid4()),
        cancelled=True,
        processes_killed=0,
        message="Session cancelled",
        checkpoint_id="cp-abc",
    )
    assert body.checkpoint_id == "cp-abc"
    dumped = body.model_dump()
    assert dumped["checkpoint_id"] == "cp-abc"


@pytest.mark.asyncio
async def test_run_loop_max_turns_checkpoint_round_trip() -> None:
    script = [
        SDKMessage(type="assistant", data={"content": "partial"}),
        SDKMessage(
            type="result",
            data={"reason": "max_turns", "session_id": "s", "usage": {"total_tokens": 9}},
        ),
    ]
    engine = _FakeEngine(script)
    store = InMemoryCheckpointStore()
    state = RunState(session_id="s", agent_name="test_agent")

    checkpoint_id = None
    async for ev in run_loop(engine, "hi", run_state=state, checkpoint_store=store):
        if ev.type == "result":
            checkpoint_id = ev.data.get("checkpoint_id")

    assert checkpoint_id
    loaded = await store.load(checkpoint_id)
    assert loaded is not None
    assert loaded.reason == "max_turns"

    # Resume contract: seed from checkpoint messages.
    cp = create_checkpoint(
        session_id="s",
        agent_name="test_agent",
        turn=1,
        messages=loaded.messages,
        reason="max_turns",
    )
    await store.save(cp)
    again = await store.load(cp.checkpoint_id)
    assert again is not None
    assert again.messages == loaded.messages
