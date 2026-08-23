"""Wiring for the periodic knowledge-base garbage collection loop."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from StAgent.config.settings import Settings
from StAgent.services.service_manager import ServiceManager


def _manager(**knowledge: Any) -> ServiceManager:
    settings = Settings()
    for key, value in knowledge.items():
        setattr(settings.knowledge, key, value)
    manager = ServiceManager(settings)
    manager._db = object()  # type: ignore[assignment]
    return manager


@pytest.mark.asyncio
async def test_gc_task_spawned_when_enabled() -> None:
    manager = _manager(gc_interval_minutes=60.0)
    try:
        manager._start_knowledge_gc()
        assert manager._knowledge_gc_task is not None
        assert manager._knowledge_gc_task.get_name() == "knowledge-gc"
    finally:
        await manager._stop_knowledge_gc()


def test_gc_disabled_interval_spawns_no_task() -> None:
    manager = _manager(gc_interval_minutes=0.0)
    manager._start_knowledge_gc()
    assert manager._knowledge_gc_task is None


def test_gc_without_database_spawns_no_task() -> None:
    manager = ServiceManager(Settings())
    manager._start_knowledge_gc()
    assert manager._knowledge_gc_task is None


@pytest.mark.asyncio
async def test_gc_loop_collects_then_stops_cleanly(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = _manager(gc_interval_minutes=60.0)
    calls: list[dict[str, Any]] = []
    sleeps: list[float] = []
    real_sleep = asyncio.sleep

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)
        await real_sleep(0.005)

    class Collector:
        def __init__(self, _db: object) -> None:
            return None

        async def collect(self) -> dict[str, Any]:
            calls.append({"ran": True})
            return {"deleted_chunks": 0}

    monkeypatch.setattr("StAgent.knowledge.gc.KnowledgeGarbageCollector", Collector)
    monkeypatch.setattr("StAgent.services.service_manager.asyncio.sleep", fake_sleep)

    manager._knowledge_gc_task = asyncio.create_task(manager._knowledge_gc_loop(60.0))
    for _ in range(200):
        if calls:
            break
        await real_sleep(0.01)
    await real_sleep(0.02)

    assert len(calls) >= 1
    # First delay is the bounded boot delay; subsequent ones are the full interval.
    assert sleeps[0] == pytest.approx(5.0 * 60.0)
    assert sleeps[1] == pytest.approx(60.0 * 60.0)

    await manager._stop_knowledge_gc()
    assert manager._knowledge_gc_task is None
