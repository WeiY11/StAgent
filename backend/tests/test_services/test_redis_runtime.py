"""Deterministic Redis runtime tests with in-memory client doubles."""

from __future__ import annotations

import pytest

from StAgent.config.settings import RedisSettings, Settings
from StAgent.runtime.run_store import get_run_store, reset_run_store
from StAgent.services.cache.redis_backend import build_redis_clients
from StAgent.services.cache.service import CacheService
from StAgent.services.service_manager import ServiceManager


class FakeAsyncRedis:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}
        self.expires: dict[str, int | None] = {}
        self.ping_calls = 0
        self.closed = False
        self.fail_get = False

    async def ping(self) -> bool:
        self.ping_calls += 1
        return True

    async def get(self, key: str) -> bytes | None:
        if self.fail_get:
            raise ConnectionError("temporary redis outage")
        return self.values.get(str(key))

    async def set(self, key: str, value: bytes, *, ex: int | None = None) -> bool:
        self.values[str(key)] = bytes(value)
        self.expires[str(key)] = ex
        return True

    async def delete(self, *keys: str) -> int:
        removed = 0
        for key in keys:
            if self.values.pop(str(key), None) is not None:
                removed += 1
            self.expires.pop(str(key), None)
        return removed

    async def exists(self, key: str) -> int:
        return int(str(key) in self.values)

    async def scan(
        self,
        cursor: int = 0,
        *,
        match: str | None = None,
        count: int | None = None,
    ) -> tuple[int, list[str]]:
        del cursor, count
        prefix = (match or "").rstrip("*")
        return 0, [key for key in self.values if key.startswith(prefix)]

    async def incrby(self, key: str, amount: int) -> int:
        next_value = int((self.values.get(str(key)) or b"0").decode("utf-8")) + amount
        self.values[str(key)] = str(next_value).encode("utf-8")
        return next_value

    async def expire(self, key: str, seconds: int) -> bool:
        self.expires[str(key)] = seconds
        return True

    async def aclose(self) -> None:
        self.closed = True


class FakeSyncRedis:
    def __init__(self) -> None:
        self.ping_calls = 0
        self.closed = False

    def ping(self) -> bool:
        self.ping_calls += 1
        return True

    def close(self) -> None:
        self.closed = True


def _settings() -> Settings:
    settings = Settings(
        redis=RedisSettings(enabled=True, url="redis://cache.example.test:6379/0"),
    )
    settings.cache.ttl_jitter_ratio = 0.0
    return settings


@pytest.mark.asyncio
async def test_redis_cache_is_l2_with_memory_fallback() -> None:
    client = FakeAsyncRedis()
    first = CacheService(_settings(), redis_client=client)
    await first.start()
    try:
        assert first.using_redis is True
        assert await first.set("q", {"answer": "grounded"}, namespace="kb", ttl=120)

        second = CacheService(_settings(), redis_client=client)
        await second.start()
        try:
            assert await second.get("q", namespace="kb") == {"answer": "grounded"}
            assert any(ttl == 120 for ttl in client.expires.values())
        finally:
            await second.stop()
    finally:
        await first.stop()


@pytest.mark.asyncio
async def test_redis_cache_retains_opaque_values_only_in_l1_and_degrades_on_outage() -> None:
    client = FakeAsyncRedis()
    service = CacheService(_settings(), redis_client=client)
    await service.start()
    opaque = object()
    try:
        assert await service.set("opaque", opaque, namespace="kb") is True
        assert await service.get("opaque", namespace="kb") is opaque

        client.fail_get = True
        assert await service.get("miss", namespace="kb", default="fallback") == "fallback"
        assert service.using_redis is False
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_redis_generation_counter_is_shared_between_workers() -> None:
    client = FakeAsyncRedis()
    first = CacheService(_settings(), redis_client=client)
    second = CacheService(_settings(), redis_client=client)
    await first.start()
    await second.start()
    try:
        assert await first.bump_generation("space-1", namespace="knowledge") == 1
        assert await second.get_generation("space-1", namespace="knowledge") == 1
    finally:
        await first.stop()
        await second.stop()


@pytest.mark.asyncio
async def test_redis_client_factory_pings_injected_async_and_sync_clients() -> None:
    async_client = FakeAsyncRedis()
    sync_client = FakeSyncRedis()

    built_async, built_sync = await build_redis_clients(
        RedisSettings(enabled=True, url="redis://cache.example.test:6379/0"),
        async_client=async_client,
        sync_client=sync_client,
    )

    assert built_async is async_client
    assert built_sync is sync_client
    assert async_client.ping_calls == 1
    assert sync_client.ping_calls == 1


@pytest.mark.asyncio
async def test_service_manager_installs_redis_run_store_for_multi_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async_client = FakeAsyncRedis()
    sync_client = FakeSyncRedis()

    async def build(_settings: RedisSettings) -> tuple[FakeAsyncRedis, FakeSyncRedis]:
        return async_client, sync_client

    monkeypatch.setattr("StAgent.services.cache.redis_backend.build_redis_clients", build)
    settings = _settings()
    settings.workers = 2
    manager = ServiceManager(settings)
    reset_run_store()

    await manager._start_cache()
    try:
        assert manager.redis is async_client
        assert manager.redis_client is sync_client
        assert manager.cache is not None and manager.cache.using_redis is True
        assert type(get_run_store()).__name__ == "RedisRunStore"
    finally:
        await manager.stop_all()
        reset_run_store()
