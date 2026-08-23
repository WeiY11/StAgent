"""Pluggable cache providers for workflow execution.

Lifecycle hooks mirror the runner flow:

- ``on_prompt_start(prompt_id)`` — begin tracking writes for this prompt.
- ``on_lookup(key)`` — returns a cached value or ``None`` on miss.
- ``should_cache(key, node_def)`` — filters which nodes we persist.
- ``on_store(key, value)`` — write a produced value.
- ``on_prompt_end(prompt_id)`` — finalize.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from StAgent.services.cache.service import CacheService

logger = logging.getLogger(__name__)

WORKFLOW_NODE_OUTPUT_CACHE_NAMESPACE = "workflow_node_output"


class CacheProvider(ABC):
    @abstractmethod
    async def on_prompt_start(self, prompt_id: str) -> None: ...

    @abstractmethod
    async def on_prompt_end(self, prompt_id: str) -> None: ...

    @abstractmethod
    async def on_lookup(self, key: str) -> Any | None: ...

    @abstractmethod
    async def on_store(self, key: str, value: Any) -> None: ...

    def should_cache(self, key: str, node_def: dict[str, Any]) -> bool:
        return True


class NullCacheProvider(CacheProvider):
    async def on_prompt_start(self, prompt_id: str) -> None:
        return

    async def on_prompt_end(self, prompt_id: str) -> None:
        return

    async def on_lookup(self, key: str) -> Any | None:
        return None

    async def on_store(self, key: str, value: Any) -> None:
        return


class CacheServiceWorkflowProvider(CacheProvider):
    """Workflow output cache provider backed by the shared CacheService.

    The executor still owns its per-process bounded cache. This provider is a
    second-level process cache so repeated workflow executions can reuse safe,
    fingerprinted node outputs without bypassing CacheService limits, TTLs, and
    observability.
    """

    def __init__(
        self,
        cache: CacheService,
        *,
        namespace: str = WORKFLOW_NODE_OUTPUT_CACHE_NAMESPACE,
        ttl: int | None = None,
    ) -> None:
        self._cache = cache
        self._namespace = namespace
        self._ttl = ttl

    async def on_prompt_start(self, prompt_id: str) -> None:
        return

    async def on_prompt_end(self, prompt_id: str) -> None:
        return

    async def on_lookup(self, key: str) -> Any | None:
        try:
            return await self._cache.get(key, namespace=self._namespace)
        except Exception as exc:  # noqa: BLE001
            logger.debug("workflow_cache_provider_lookup_failed: %s", exc)
            return None

    async def on_store(self, key: str, value: Any) -> None:
        try:
            if self._ttl is None:
                await self._cache.set(key, value, namespace=self._namespace)
            else:
                await self._cache.set(
                    key,
                    value,
                    namespace=self._namespace,
                    ttl=self._ttl,
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug("workflow_cache_provider_store_failed: %s", exc)
