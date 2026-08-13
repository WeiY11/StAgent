from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from leagent.api.schemas.chat import ChatCompletionMessage, ChatCompletionRequest
from leagent.db.models.message import MessageRole


def test_build_agent_controller_accepts_completion_config(monkeypatch: pytest.MonkeyPatch) -> None:
    from leagent.api.v1.chat_deps import build_agent_controller

    fake_ctx = SimpleNamespace(
        llm=object(),
        tools=object(),
        executor=SimpleNamespace(service_manager=None),
        agent_memory=None,
        session_manager=None,
        hook_manager=None,
        permission_context=None,
        checkpoint_store=None,
    )
    fake_sm = SimpleNamespace(llm_service=object(), runtime_context=fake_ctx)
    monkeypatch.setattr(
        "leagent.services.runtime.get_service_manager",
        lambda: fake_sm,
    )

    agent = build_agent_controller(
        prompt_variant="default_agent",
        extra_system_prompt="caller instructions",
        model_name="custom-model",
        temperature=0.2,
    )

    assert agent is not None
    assert agent.config.prompt_variant == "default_agent"
    assert agent.config.extra_system_prompt == "caller instructions"
    assert agent.config.model_name == "custom-model"
    assert agent.config.temperature == 0.2


@pytest.mark.asyncio
async def test_openai_sse_forwards_completion_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from leagent.api.v1.chat import openai_compat

    captured: dict[str, object] = {}

    def _fake_build_agent_controller(**kwargs):  # noqa: ANN001
        captured.update(kwargs)

    monkeypatch.setattr(
        openai_compat,
        "build_agent_controller",
        _fake_build_agent_controller,
    )

    req = ChatCompletionRequest(
        model="custom-model",
        messages=[ChatCompletionMessage(role=MessageRole.USER, content="hello")],
        stream=True,
        temperature=0.3,
        system_prompt="caller instructions",
        agent_variant="default_agent",
    )
    chat_svc = SimpleNamespace(add_message=AsyncMock())
    gen = openai_compat._generate_openai_sse(req, uuid4(), uuid4(), chat_svc)
    try:
        await gen.__anext__()
        await gen.__anext__()
    finally:
        await gen.aclose()

    assert captured == {
        "prompt_variant": "default_agent",
        "extra_system_prompt": "caller instructions",
        "model_name": "custom-model",
        "temperature": 0.3,
    }


@pytest.mark.asyncio
async def test_openai_sse_releases_knowledge_lease_when_client_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Closing the SSE iterator must exit its S3 knowledge materialization lease."""
    from leagent.api.v1.chat import openai_compat

    lease_state = {"entered": False, "exited": False}
    seen_attachments: list[str] | None = None

    @asynccontextmanager
    async def _knowledge_lease(*_args: object, **_kwargs: object):
        lease_state["entered"] = True
        try:
            yield ["C:/temporary/knowledge-source.txt"]
        finally:
            lease_state["exited"] = True

    class _Agent:
        pass

    async def _stream(*_args: object, **kwargs: object):
        nonlocal seen_attachments
        raw = kwargs.get("attachments")
        seen_attachments = list(raw) if isinstance(raw, list) else None
        yield "token", {"token": "first"}, "first"
        yield "token", {"token": "second"}, "firstsecond"

    monkeypatch.setattr(openai_compat, "build_agent_controller", lambda **_kwargs: _Agent())
    monkeypatch.setattr(openai_compat, "_materialize_knowledge_message_paths", _knowledge_lease)
    monkeypatch.setattr(openai_compat, "_run_agent_stream", _stream)
    monkeypatch.setattr(
        openai_compat,
        "_authorized_root_paths_for_session",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "leagent.services.chat.pet_personality.apply_pet_personality_to_agent",
        AsyncMock(),
    )

    req = ChatCompletionRequest(
        model="custom-model",
        messages=[ChatCompletionMessage(role=MessageRole.USER, content="@knowledge:x")],
        stream=True,
    )
    gen = openai_compat._generate_openai_sse(
        req,
        uuid4(),
        uuid4(),
        SimpleNamespace(add_message=AsyncMock()),
        db=SimpleNamespace(),
    )
    try:
        await gen.__anext__()
        await gen.__anext__()
    finally:
        await gen.aclose()

    assert lease_state == {"entered": True, "exited": True}
    assert seen_attachments is not None
    assert Path(seen_attachments[0]) == Path("C:/temporary/knowledge-source.txt")
