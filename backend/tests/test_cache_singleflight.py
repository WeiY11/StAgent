from __future__ import annotations

import asyncio

import pytest

from StAgent.config.settings import Settings
from StAgent.services.cache.service import CacheService


@pytest.mark.asyncio
async def test_get_or_set_coalesces_concurrent_factory_calls() -> None:
    service = CacheService(Settings())
    calls = 0

    async def factory() -> str:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        return "value"

    values = await asyncio.gather(*[
        service.get_or_set("k", factory, namespace="chat") for _ in range(50)
    ])
    assert values == ["value"] * 50
    assert calls == 1


@pytest.mark.asyncio
async def test_failed_fill_is_not_cached_and_next_call_retries() -> None:
    service = CacheService(Settings())
    calls = 0

    async def factory() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("boom")
        return "recovered"

    with pytest.raises(RuntimeError, match="boom"):
        await service.get_or_set("k", factory, namespace="chat")
    assert await service.get_or_set("k", factory, namespace="chat") == "recovered"


@pytest.mark.asyncio
async def test_generation_increment_is_atomic() -> None:
    service = CacheService(Settings())
    values = await asyncio.gather(*[
        service.bump_generation("session:s1", namespace="chat")
        for _ in range(100)
    ])
    assert sorted(values) == list(range(1, 101))
    assert await service.get_generation("session:s1", namespace="chat") == 100


def test_ttl_jitter_is_stable_and_within_configured_band() -> None:
    settings = Settings()
    settings.cache.ttl_jitter_ratio = 0.10
    service = CacheService(settings)
    first = service._effective_ttl("stable-key", 100.0)
    second = service._effective_ttl("stable-key", 100.0)
    assert first == second
    assert 90.0 <= first <= 110.0
