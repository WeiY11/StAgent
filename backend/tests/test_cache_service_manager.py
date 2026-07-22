from __future__ import annotations

import pytest

from leagent.config.settings import Settings
from leagent.services.base import ServiceState
from leagent.services.service_manager import ServiceManager


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
