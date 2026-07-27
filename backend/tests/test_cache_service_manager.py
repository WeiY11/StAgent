from __future__ import annotations

from typing import Any

import pytest

from leagent.config.settings import Settings
from leagent.services.base import ServiceState
from leagent.services.service_manager import ServiceManager
from leagent.workflow.engine import CacheServiceWorkflowProvider


@pytest.mark.asyncio
async def test_cache_service_lifecycle_is_owned_by_manager() -> None:
    manager = ServiceManager(Settings())
    await manager._start_cache()
    assert manager.cache is not None
    assert manager.cache.state is ServiceState.READY
    await manager.cache.stop()


@pytest.mark.asyncio
async def test_chat_receives_manager_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = ServiceManager(Settings())
    manager._db = object()  # type: ignore[assignment]
    await manager._start_cache()
    captured = {}

    async def fake_init(settings, database, cache):  # noqa: ANN001
        captured["cache"] = cache
        return object()

    monkeypatch.setattr("leagent.services.chat.service.init_chat_service", fake_init)
    await manager._start_chat()
    assert captured["cache"] is manager.cache
    await manager.cache.stop()


@pytest.mark.asyncio
async def test_session_manager_receives_manager_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = ServiceManager(Settings())
    manager._db = object()  # type: ignore[assignment]
    await manager._start_cache()
    captured = {}

    class FakeSessionManager:
        def __init__(self, settings, *, database, file_service=None, cache=None):  # noqa: ANN001
            captured["cache"] = cache

    monkeypatch.setattr(
        "leagent.services.session.manager.SessionManager",
        FakeSessionManager,
    )
    await manager._start_session_manager()
    assert captured["cache"] is manager.cache
    await manager.cache.stop()


@pytest.mark.asyncio
async def test_workflow_executor_receives_cache_service_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = ServiceManager(Settings())
    manager._db = object()  # type: ignore[assignment]
    manager._runtime_context = type("RuntimeContextStub", (), {"executor": object()})()  # type: ignore[assignment]
    await manager._start_cache()
    captured: dict[str, Any] = {}

    async def fake_bootstrap(*args, **kwargs):  # noqa: ANN002, ANN003
        return None

    class FakeEventBus:
        async def publish_event(self, prompt_id, event):  # noqa: ANN001
            return None

    class FakeWorkflowExecutor:
        def __init__(self, **kwargs):  # noqa: ANN003
            captured.update(kwargs)

        def register_progress_handler(self, handler):  # noqa: ANN001
            captured["progress_handler"] = handler

    class FakeWorkflowService:
        def __init__(self, **kwargs):  # noqa: ANN003
            captured["workflow_service_kwargs"] = kwargs

    monkeypatch.setattr("leagent.workflow.nodes.bootstrap", fake_bootstrap)
    monkeypatch.setattr("leagent.workflow.server.event_bus.InMemoryEventBus", FakeEventBus)
    monkeypatch.setattr("leagent.workflow.engine.WorkflowExecutor", FakeWorkflowExecutor)
    monkeypatch.setattr("leagent.workflow.registry.FlowWorkflowRegistry", lambda db: object())
    monkeypatch.setattr("leagent.workflow.state_store.build_workflow_state_store", lambda db: object())
    monkeypatch.setattr("leagent.workflow.prompt_map.build_prompt_map", lambda value: object())
    monkeypatch.setattr("leagent.workflow.services.WorkflowService", FakeWorkflowService)
    monkeypatch.setattr("leagent.tools.registry.get_registry", lambda: object())

    await manager._start_workflow_service()

    provider = captured["cache_provider"]
    assert isinstance(provider, CacheServiceWorkflowProvider)
    assert provider._cache is manager.cache
    assert manager.workflow_service is not None
    await manager.cache.stop()
