"""Optional Redis L2 adapter for :mod:`leagent.services.cache`.

Only JSON-safe cache values are replicated across processes.  Opaque Python
objects stay in the local L1 cache instead of being serialized with pickle,
which prevents a Redis value from becoming a code-execution boundary.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import math
from typing import TYPE_CHECKING, Any

import orjson

if TYPE_CHECKING:
    from leagent.config.settings import RedisSettings

logger = logging.getLogger(__name__)


def _to_text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return str(value)


class RedisCacheBackend:
    """Async Redis cache adapter with JSON-only value replication."""

    def __init__(self, client: Any, *, prefix: str = "leagent") -> None:
        self._client = client
        self._prefix = prefix.rstrip(":") or "leagent"

    @property
    def client(self) -> Any:
        return self._client

    @property
    def prefix(self) -> str:
        return self._prefix

    def _key(self, key: str) -> str:
        return f"{self._prefix}:cache:{key}"

    async def ping(self) -> None:
        result = await self._client.ping()
        if result is False:
            raise RuntimeError("Redis ping returned false")

    async def get(self, key: str) -> tuple[bool, Any]:
        raw = await self._client.get(self._key(key))
        if raw is None:
            return False, None
        try:
            return True, orjson.loads(raw)
        except (orjson.JSONDecodeError, TypeError):
            # Treat malformed values as misses. They may come from a previous
            # incompatible deployment; delete them without ever exposing a
            # non-JSON payload to an application caller.
            await self._client.delete(self._key(key))
            return False, None

    async def set(self, key: str, value: Any, *, ttl: float | None) -> bool:
        try:
            payload = orjson.dumps(value)
        except (TypeError, ValueError):
            return False
        redis_key = self._key(key)
        if ttl is not None and ttl <= 0:
            await self._client.delete(redis_key)
            return True
        expires_in = math.ceil(ttl) if ttl is not None else None
        await self._client.set(redis_key, payload, ex=expires_in)
        return True

    async def delete(self, key: str) -> bool:
        return bool(await self._client.delete(self._key(key)))

    async def exists(self, key: str) -> bool:
        return bool(await self._client.exists(self._key(key)))

    async def delete_prefix(self, prefix: str) -> int:
        """Delete a prefix with SCAN, never blocking Redis with KEYS."""
        cursor: int | str = 0
        removed = 0
        match = f"{self._key(prefix)}*"
        while True:
            cursor, keys = await self._client.scan(cursor=cursor, match=match, count=500)
            if keys:
                removed += int(await self._client.delete(*[_to_text(key) for key in keys]))
            if str(cursor) == "0":
                break
        return removed

    async def increment(self, key: str, amount: int, *, ttl: float | None) -> int:
        """Atomically increment a cross-worker counter and refresh its TTL."""
        redis_key = self._key(key)
        value = int(await self._client.incrby(redis_key, amount))
        if ttl is not None and ttl > 0:
            await self._client.expire(redis_key, math.ceil(ttl))
        elif ttl is not None:
            await self._client.delete(redis_key)
            return 0
        return value

    async def close(self) -> None:
        close = getattr(self._client, "aclose", None) or getattr(self._client, "close", None)
        if not callable(close):
            return
        result = close()
        if inspect.isawaitable(result):
            await result


async def build_redis_clients(
    settings: RedisSettings,
    *,
    async_client: Any | None = None,
    sync_client: Any | None = None,
) -> tuple[Any, Any]:
    """Build and health-check the async cache and sync run-store clients.

    A passed client is primarily useful for tests and application-specific
    client factories.  Production imports redis-py lazily so the standalone
    SQLite profile does not acquire a Redis dependency.
    """
    if not settings.enabled:
        raise ValueError("Redis runtime is disabled")
    if async_client is None or sync_client is None:
        try:
            import redis
            import redis.asyncio as redis_async
        except ImportError as exc:
            raise RuntimeError(
                "Redis runtime requires the production dependency extra "
                "(uv sync --extra production)"
            ) from exc
        kwargs = {
            "decode_responses": False,
            "socket_connect_timeout": settings.socket_timeout_seconds,
            "socket_timeout": settings.socket_timeout_seconds,
            "health_check_interval": 30,
        }
        if async_client is None:
            async_client = redis_async.from_url(settings.url, **kwargs)
        if sync_client is None:
            sync_client = redis.Redis.from_url(settings.url, **kwargs)
    try:
        async_ping = await async_client.ping()
        sync_ping = await asyncio.to_thread(sync_client.ping)
        if async_ping is False or sync_ping is False:
            raise RuntimeError("Redis ping returned false")
    except Exception:
        await _close_async_client(async_client)
        _close_sync_client(sync_client)
        raise
    return async_client, sync_client


async def _close_async_client(client: Any) -> None:
    close = getattr(client, "aclose", None) or getattr(client, "close", None)
    if not callable(close):
        return
    result = close()
    if inspect.isawaitable(result):
        await result


def _close_sync_client(client: Any) -> None:
    close = getattr(client, "close", None)
    if callable(close):
        close()


__all__ = ["RedisCacheBackend", "build_redis_clients"]
