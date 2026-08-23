# StAgent Cache Mechanism Optimization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把当前分散、部分未接线的进程内缓存收敛成一个有界、可观测、并发安全、按用户与版本隔离的缓存机制，同时保持 SQLite 单进程默认部署零外部依赖。

**Architecture:** `CacheService` 是应用级缓存控制面，底层继续使用进程内 `BoundedLRU/ShardedLRU`；数据库与文件存储始终是事实源。聊天、文件、变量、Embedding、Recall 使用同一服务，但会话状态保留专用的小型 L1，并改用共享 LRU 原语。可变数据采用“数据库提交成功后 bump generation”，读取键包含 generation；昂贵填充使用 single-flight。Redis 不在本计划中落地，只通过稳定接口预留后续 PostgreSQL 多 worker 扩展点。

**Tech Stack:** Python 3.11+、FastAPI、Pydantic v2、SQLModel/SQLAlchemy async、SQLite WAL、pytest/pytest-asyncio、Prometheus client、现有 `uv` 环境。

## Global Constraints

- 默认部署继续是 SQLite、单进程、无 Redis；不得新增必需的外部服务或网络依赖。
- 数据库和文件层是唯一事实源；缓存写入失败只能降低性能，不能让业务请求失败或产生数据库不存在的状态。
- 缓存键必须包含 schema version、owner scope、resource scope、generation 和 variant digest；不得跨用户、跨工作区、跨模型配置复用。
- 缓存不得保存鉴权结果、审批结果、一次性 token、明文密钥、超过自身过期时间的签名 URL。
- 通用缓存单值默认不超过 1 MiB；大型文件、媒体和工作流产物只缓存 `FileRef`/`MediaRef` 或稳定 ID。
- 所有 TTL 使用单调时钟；`ttl=None` 表示不过期，`ttl<=0` 表示立即过期。
- 所有新 Python 签名必须带类型标注；I/O 保持 `async`；格式遵循 Black 与 Ruff。
- 当前工作树有用户未提交修改。每次提交只能显式暂存本任务文件，禁止 `git add .`，禁止覆盖或回滚无关改动。
- 每个任务先写失败测试，再写最小实现；任务结束时必须执行列出的定向测试。
- 命中率不能单独作为上线依据；验收同时检查正确性、p95、驻留条目、拒绝写入、淘汰和数据库回退。

## Scope Boundary

本计划完成本地单进程缓存的正确性和性能闭环，并定义后续共享后端接口。只有同时满足以下条件，才单独启动 Redis L2 子项目：`DATABASE_URL` 指向 PostgreSQL、`StAgent_WORKERS>1`、压测证明跨 worker 重算是主要瓶颈。Redis 子项目必须另写计划，覆盖序列化、失效广播、超时、熔断和多 worker 一致性；本计划不安装 Redis 包。

## File Map

### New files

- `backend/StAgent/services/cache/keys.py`：生成不可串租户、可版本化的逻辑缓存键。
- `backend/tests/test_cache_keys.py`：键稳定性、隔离性、规范化测试。
- `backend/tests/test_cache_service_manager.py`：验证缓存服务生命周期和依赖注入。
- `backend/tests/test_cache_singleflight.py`：并发填充、取消和异常恢复测试。
- `backend/tests/test_cache_integration.py`：聊天 generation、Embedding、Recall、工作流隔离的跨组件验证。
- `backend/tests/test_stats_api.py`：缓存统计 API 响应契约。
- `backend/tests/test_cache_metrics.py`：低基数 Prometheus 指标契约。
- `backend/tests/bench_cache_hot_paths.py`：可重复的冷/热缓存基准，不进入默认单测。
- `docs/technical/cache-architecture.md`：运行语义、键规则、TTL 和扩展边界。

### Existing files to modify

- `backend/StAgent/config/settings.py`：容量、单值大小、清扫周期、TTL 抖动和配置约束。
- `backend/StAgent/services/cache/lru.py`：单调 TTL、原子更新、精确分片容量。
- `backend/StAgent/services/cache/service.py`：single-flight、generation、容量拒绝、维护任务。
- `backend/StAgent/services/cache/observability.py`：拒绝、填充、合并和最近窗口指标。
- `backend/StAgent/services/cache/__init__.py`：导出新增公共接口。
- `backend/StAgent/services/service_manager.py`：启动、注入、健康检查和停止缓存服务。
- `backend/StAgent/services/chat/service.py`：聊天历史 generation 键与提交后失效。
- `backend/StAgent/services/session/manager.py`：移除未使用的 cache 参数。
- `backend/StAgent/services/session/store.py`：共享有界 LRU 原语，数据库成功后才发布缓存。
- `backend/StAgent/context/cache.py`、`backend/StAgent/context/manager.py`：有界 TTL 和统一观测。
- `backend/StAgent/skills/resource_cache.py`、`backend/StAgent/skills/bundle_payload_cache.py`：线程安全、单调 TTL、统一统计。
- `backend/StAgent/tools/web/web_search/cache.py`：复用共享 LRU 并保留 Web TTL 门面。
- `backend/StAgent/memory/embeddings.py`：使用 `CacheService` 和 single-flight。
- `backend/StAgent/memory/recall.py`、`backend/StAgent/memory/agent_memory.py`：按 owner/generation 缓存 Recall，成功写后 bump。
- `backend/StAgent/memory/cache.py`：删除未进入生产路径的第二套 memory cache。
- `backend/StAgent/workflow/engine/caching.py`、`backend/StAgent/workflow/engine/executor.py`、`backend/StAgent/workflow/engine/runner.py`：有界默认值、执行范围隔离、大小观测。
- `backend/StAgent/api/v1/stats.py`：扩展只读统计；运行时配置端点增加边界验证并明确不持久化。
- `deploy/.env.example`：缓存配置示例。

---

### Task 1: Freeze cache-key and configuration contracts

**Files:**
- Create: `backend/StAgent/services/cache/keys.py`
- Create: `backend/tests/test_cache_keys.py`
- Modify: `backend/StAgent/config/settings.py:460-503`
- Modify: `backend/StAgent/services/cache/__init__.py`

**Interfaces:**
- Produces: `build_scoped_cache_key(logical_key: str, *, owner_id: object | None = None, resource_id: object | None = None, generation: int = 0, vary: Mapping[str, Any] | None = None) -> str`
- Produces: `CacheSettings.max_value_bytes`, `maintenance_interval_sec`, `ttl_jitter_ratio` and validation that `max_items >= shard_count`.
- Consumes: existing Pydantic v2 settings and `hashlib/json` only.

- [ ] **Step 1: Write key-contract and settings tests**

```python
# backend/tests/test_cache_keys.py
from pydantic import ValidationError
import pytest

from StAgent.config.settings import CacheSettings
from StAgent.services.cache.keys import build_scoped_cache_key


def test_scoped_key_is_stable_for_canonical_vary_order() -> None:
    left = build_scoped_cache_key(
        "messages", owner_id="u1", resource_id="s1", generation=7,
        vary={"limit": 50, "before": None},
    )
    right = build_scoped_cache_key(
        "messages", owner_id="u1", resource_id="s1", generation=7,
        vary={"before": None, "limit": 50},
    )
    assert left == right


def test_scoped_key_isolates_owner_resource_and_generation() -> None:
    base = build_scoped_cache_key("messages", owner_id="u1", resource_id="s1")
    assert base != build_scoped_cache_key("messages", owner_id="u2", resource_id="s1")
    assert base != build_scoped_cache_key("messages", owner_id="u1", resource_id="s2")
    assert base != build_scoped_cache_key(
        "messages", owner_id="u1", resource_id="s1", generation=1
    )


def test_cache_settings_reject_more_shards_than_items() -> None:
    with pytest.raises(ValidationError):
        CacheSettings(max_items=2, shard_count=4)
```

- [ ] **Step 2: Run the new tests and verify collection fails**

Run:

```powershell
$env:UV_CACHE_DIR='D:\StAgent\.uv-cache'
uv run pytest tests/test_cache_keys.py -q
```

Expected: FAIL because `StAgent.services.cache.keys` and the new settings fields do not exist.

- [ ] **Step 3: Add the scoped key builder**

```python
# backend/StAgent/services/cache/keys.py
from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

_KEY_SCHEMA = "v2"


def _digest(value: object | None, *, size: int = 16) -> str:
    raw = "-" if value is None else str(value)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:size]


def build_scoped_cache_key(
    logical_key: str,
    *,
    owner_id: object | None = None,
    resource_id: object | None = None,
    generation: int = 0,
    vary: Mapping[str, Any] | None = None,
) -> str:
    """Return a stable key isolated by owner, resource and data generation."""
    payload = json.dumps(
        dict(vary or {}), sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, default=str,
    )
    return ":".join(
        (
            _KEY_SCHEMA,
            f"o{_digest(owner_id)}",
            f"r{_digest(resource_id)}",
            f"g{max(0, int(generation))}",
            f"k{_digest(logical_key, size=24)}",
            f"x{_digest(payload, size=24)}",
        )
    )
```

- [ ] **Step 4: Extend and validate `CacheSettings`**

```python
# backend/StAgent/config/settings.py imports
from pydantic import AliasChoices, Field, model_validator

# inside CacheSettings
max_value_bytes: int = Field(default=1_048_576, ge=1_024)
maintenance_interval_sec: float = Field(default=30.0, ge=0.0, le=3_600.0)
ttl_jitter_ratio: float = Field(default=0.05, ge=0.0, le=0.25)

@model_validator(mode="after")
def validate_cache_capacity(self) -> "CacheSettings":
    if self.max_items < self.shard_count:
        raise ValueError("cache max_items must be >= shard_count")
    return self
```

Export `build_scoped_cache_key` from `backend/StAgent/services/cache/__init__.py`.

- [ ] **Step 5: Run key and existing configuration tests**

Run:

```powershell
uv run pytest tests/test_cache_keys.py tests/test_cache_optimization.py::test_namespace_ttls_differ_from_default -q
```

Expected: all tests PASS.

- [ ] **Step 6: Commit the contract**

```powershell
git add backend/StAgent/services/cache/keys.py backend/StAgent/services/cache/__init__.py backend/StAgent/config/settings.py backend/tests/test_cache_keys.py
git commit -m "feat(cache): define scoped keys and capacity constraints"
```

---

### Task 2: Make the shared LRU primitive precise and atomic

**Files:**
- Modify: `backend/StAgent/services/cache/lru.py`
- Modify: `backend/tests/test_cache_primitives.py`

**Interfaces:**
- Produces: `BoundedLRU.update(key, updater, *, default=None, ttl=None) -> tuple[Any, int]`.
- Produces: `ShardedLRU.update(...)` with identical semantics.
- Preserves: `get`, `peek`, `set`, `delete`, `delete_prefix`, `resize`, `stats`.

- [ ] **Step 1: Add failing tests for exact capacity, monotonic expiry and atomic updates**

```python
# append to backend/tests/test_cache_primitives.py
def test_sharded_capacity_equals_requested_capacity() -> None:
    lru = ShardedLRU(max_size=10, shards=3)
    assert lru.max_size == 10


def test_atomic_update_does_not_lose_concurrent_increments() -> None:
    lru = BoundedLRU(max_size=4)

    def worker() -> None:
        for _ in range(1_000):
            lru.update("counter", lambda value: int(value) + 1, default=0)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert lru.get("counter") == (8_000, True)


def test_zero_ttl_is_immediately_expired() -> None:
    lru = BoundedLRU(max_size=2)
    lru.set("k", "v", ttl=0)
    assert lru.get("k") == (None, False)
```

- [ ] **Step 2: Run the three tests and verify failures**

Run:

```powershell
uv run pytest tests/test_cache_primitives.py -q
```

Expected: FAIL on missing `update` and incorrect sharded `max_size`.

- [ ] **Step 3: Switch expiry bookkeeping to a monotonic clock**

Replace entry creation and lookup time calls in `lru.py`:

```python
class _Entry:
    __slots__ = ("value", "expires_at", "created_at")

    def __init__(self, value: Any, expires_at: float | None) -> None:
        self.value = value
        self.expires_at = expires_at
        self.created_at = time.monotonic()


def _expiry(ttl: float | None) -> float | None:
    return None if ttl is None else time.monotonic() + float(ttl)
```

Use `time.monotonic()` in `get`, `peek`, `keys`, `items`, `sweep_expired`; use `_expiry(ttl)` in `set` so `ttl=0` is not treated as `None`.

- [ ] **Step 4: Add atomic update under the entry lock**

```python
# inside BoundedLRU
def update(
    self,
    key: str,
    updater: Any,
    *,
    default: Any = None,
    ttl: float | None = None,
) -> tuple[Any, int]:
    now = time.monotonic()
    with self._lock:
        entry = self._data.get(key)
        if entry is None or entry.is_expired(now):
            if entry is not None:
                del self._data[key]
                self._expirations += 1
            current = default
        else:
            current = entry.value
            del self._data[key]
        value = updater(current)
        self._data[key] = _Entry(value, _expiry(ttl))
        self._sets += 1
        evicted = 0
        while len(self._data) > self._max_size:
            self._data.pop(next(iter(self._data)))
            evicted += 1
        self._lru_evictions += evicted
        return value, evicted
```

Add a delegating `update` method to `ShardedLRU`.

- [ ] **Step 5: Distribute sharded capacity without truncation**

```python
# inside ShardedLRU.__init__
n = min(max(1, int(shards)), max(1, int(max_size)))
base, extra = divmod(max(1, int(max_size)), n)
self._shards = [
    BoundedLRU(base + (1 if index < extra else 0))
    for index in range(n)
]
self._n = n
```

Use the same quotient/remainder distribution in `resize`; reject `max_size < self._n` with `ValueError` because settings and runtime validation already prohibit it.

- [ ] **Step 6: Run primitive and service cache tests**

Run:

```powershell
uv run pytest tests/test_cache_primitives.py tests/test_services.py::TestInMemoryCache -q
```

Expected: all tests PASS.

- [ ] **Step 7: Commit the primitive hardening**

```powershell
git add backend/StAgent/services/cache/lru.py backend/tests/test_cache_primitives.py
git commit -m "fix(cache): make ttl and sharded updates precise"
```

---

### Task 3: Add single-flight, atomic generations and bounded values to CacheService

**Files:**
- Modify: `backend/StAgent/services/cache/service.py`
- Modify: `backend/StAgent/services/cache/observability.py`
- Create: `backend/tests/test_cache_singleflight.py`
- Modify: `backend/tests/test_cache_optimization.py`

**Interfaces:**
- Produces: `CacheService.get_or_set(...)` that coalesces concurrent fills.
- Produces: `CacheService.get_generation(subject, *, namespace) -> int`.
- Produces: `CacheService.bump_generation(subject, *, namespace) -> int`.
- Produces: `CacheService.sweep_expired() -> int`.
- Changes: `CacheService.set(...) -> bool` returns `False` for an oversized value.

- [ ] **Step 1: Write concurrent-fill and generation tests**

```python
# backend/tests/test_cache_singleflight.py
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
```

- [ ] **Step 2: Run the new tests and verify failures**

Run:

```powershell
uv run pytest tests/test_cache_singleflight.py -q
```

Expected: FAIL because concurrent calls run the factory more than once and generation methods do not exist.

- [ ] **Step 3: Implement task-based single-flight**

Add these members in `CacheService.__init__`:

```python
self._inflight: dict[str, asyncio.Task[Any]] = {}
self._inflight_lock = asyncio.Lock()
self._maintenance_stop = asyncio.Event()
self._maintenance_task: asyncio.Task[None] | None = None
```

Replace `get_or_set` with:

```python
async def get_or_set(
    self,
    key: str,
    factory: Any,
    *,
    namespace: str | None = None,
    ttl: Any = _UNSET,
) -> Any:
    cached = await self.get(key, namespace=namespace, default=_MISS)
    if cached is not _MISS:
        return cached
    full_key = self._make_key(key, namespace)

    async with self._inflight_lock:
        task = self._inflight.get(full_key)
        if task is None:
            task = asyncio.create_task(
                self._fill(key, factory, namespace=namespace, ttl=ttl)
            )
            self._inflight[full_key] = task

    try:
        return await asyncio.shield(task)
    finally:
        if task.done():
            async with self._inflight_lock:
                if self._inflight.get(full_key) is task:
                    self._inflight.pop(full_key, None)

async def _fill(
    self, key: str, factory: Any, *, namespace: str | None, ttl: Any
) -> Any:
    cached = await self.get(key, namespace=namespace, default=_MISS)
    if cached is not _MISS:
        return cached
    value = await factory() if asyncio.iscoroutinefunction(factory) else factory()
    if asyncio.iscoroutine(value):
        value = await value
    await self.set(key, value, namespace=namespace, ttl=ttl)
    return value
```

- [ ] **Step 4: Implement atomic generation operations**

Expose `InMemoryCache.update` as an async wrapper over `self._lru.update`. Then add:

```python
async def get_generation(self, subject: str, *, namespace: str) -> int:
    value = await self.get(
        f"__generation__:{subject}", namespace=namespace, default=0
    )
    return int(value or 0)

async def bump_generation(self, subject: str, *, namespace: str) -> int:
    full_key = self._make_key(f"__generation__:{subject}", namespace)
    value, evicted = await self._memory_cache.update(
        full_key,
        lambda current: int(current or 0) + 1,
        default=0,
        ttl=None,
    )
    record_cache_set(self._stat_name(namespace), evicted=evicted)
    return int(value)
```

Rewrite `increment` to use the same atomic `update` path instead of `get` followed by `set`.

- [ ] **Step 5: Reject oversized values before insertion**

```python
# backend/StAgent/services/cache/service.py imports
import hashlib
import sys

def _estimate_value_bytes(value: Any) -> int:
    try:
        import orjson
        return len(orjson.dumps(value, default=str))
    except Exception:  # noqa: BLE001
        return sys.getsizeof(value)

# at the start of CacheService.set, after ttl resolution
value_bytes = _estimate_value_bytes(value)
if value_bytes > self._cache_cfg.max_value_bytes:
    record_cache_rejection(self._stat_name(namespace), reason="oversized")
    return False

def _effective_ttl(self, full_key: str, ttl: float | None) -> float | None:
    if ttl is None or ttl <= 0 or self._cache_cfg.ttl_jitter_ratio <= 0:
        return ttl
    raw = int(hashlib.sha256(full_key.encode("utf-8")).hexdigest()[:8], 16)
    unit = raw / 0xFFFFFFFF
    ratio = self._cache_cfg.ttl_jitter_ratio
    return ttl * (1.0 - ratio + (2.0 * ratio * unit))
```

Pass `_effective_ttl(full_key, ttl)` to `_memory_cache.set`. Deterministic jitter keeps tests repeatable and prevents a namespace's entries from expiring in one burst.

Add `rejections: int` to `CacheStat`, `record_cache_rejection(...)`, and expose it in snapshots. Do not use `reason` as a Prometheus label; keep it in the in-process detail map to avoid cardinality growth.

- [ ] **Step 6: Add periodic expired-entry sweeping**

```python
async def _maintenance_loop(self) -> None:
    interval = self._cache_cfg.maintenance_interval_sec
    while interval > 0 and not self._maintenance_stop.is_set():
        try:
            await asyncio.wait_for(self._maintenance_stop.wait(), timeout=interval)
        except TimeoutError:
            expired = await self._memory_cache.sweep_expired()
            if expired:
                record_cache_expiration("cache:_memory", count=expired)

async def _do_start(self) -> None:
    self._maintenance_stop.clear()
    if self._cache_cfg.maintenance_interval_sec > 0:
        self._maintenance_task = asyncio.create_task(self._maintenance_loop())

async def _do_stop(self) -> None:
    self._maintenance_stop.set()
    if self._maintenance_task is not None:
        await self._maintenance_task
        self._maintenance_task = None
    await self._memory_cache.clear()
```

Add `sweep_expired` to `InMemoryCache`; import and use the public observability function instead of touching registry internals.

- [ ] **Step 7: Run the service cache suite**

Run:

```powershell
uv run pytest tests/test_cache_singleflight.py tests/test_cache_optimization.py tests/test_cache_primitives.py -q
```

Expected: all tests PASS, including exactly one factory call and generation 100.

- [ ] **Step 8: Commit the service behavior**

```powershell
git add backend/StAgent/services/cache/service.py backend/StAgent/services/cache/observability.py backend/tests/test_cache_singleflight.py backend/tests/test_cache_optimization.py
git commit -m "feat(cache): coalesce fills and version mutable data"
```

---

### Task 4: Wire CacheService into the actual service lifecycle

**Files:**
- Modify: `backend/StAgent/services/service_manager.py:29-325`
- Create: `backend/tests/test_cache_service_manager.py`

**Interfaces:**
- Consumes: `init_cache_service(settings) -> CacheService` from Task 3.
- Produces: `ServiceManager.cache -> CacheService | None`.
- Injects: the same instance into `ChatService`, `VariableService`, and `FileService`.
- Does not inject: `TieredSessionStore`; Task 5 keeps one dedicated L1 there.

- [ ] **Step 1: Write lifecycle and injection tests**

```python
# backend/tests/test_cache_service_manager.py
from __future__ import annotations

import pytest

from StAgent.config.settings import Settings
from StAgent.services.base import ServiceState
from StAgent.services.service_manager import ServiceManager


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

    monkeypatch.setattr("StAgent.services.chat.service.init_chat_service", fake_init)
    await manager._start_chat()
    assert captured["cache"] is manager.cache
    await manager.cache.stop()
```

- [ ] **Step 2: Run the lifecycle tests and verify failures**

Run:

```powershell
uv run pytest tests/test_cache_service_manager.py -q
```

Expected: FAIL because `_start_cache` is missing and `cache` returns `None`.

- [ ] **Step 3: Add manager ownership and startup ordering**

```python
# ServiceManager.__init__
self._cache: "CacheService | None" = None

@property
def cache(self) -> "CacheService | None":
    return self._cache

async def _start_cache(self) -> None:
    from StAgent.services.cache.service import init_cache_service
    self._cache = await init_cache_service(self.settings)
    logger.info("CacheService initialized (memory, max_items=%d)", self.settings.cache.max_items)
```

In `start_all`, call `_start_cache()` after database and before chat. Pass `self._cache` to `init_chat_service`, `init_variable_service`, and `FileService(cache=...)`.

- [ ] **Step 4: Add health and shutdown ownership**

Add `("cache", self._cache)` to `health_check`. In `stop_all`, stop cache after chat/variable/file consumers and before database disposal:

```python
if self._cache:
    try:
        await self._cache.stop()
    except Exception:
        logger.warning("Cache service shutdown error", exc_info=True)
```

- [ ] **Step 5: Run lifecycle, service and health tests**

Run:

```powershell
uv run pytest tests/test_cache_service_manager.py tests/test_services.py -q
```

Expected: all tests PASS.

- [ ] **Step 6: Commit the runtime wiring**

```powershell
git add backend/StAgent/services/service_manager.py backend/tests/test_cache_service_manager.py
git commit -m "fix(runtime): wire cache service into application lifecycle"
```

---

### Task 5: Make chat and session invalidation correct and O(1)

**Files:**
- Modify: `backend/StAgent/services/chat/service.py:168-180,922-983`
- Modify: `backend/StAgent/services/session/manager.py:69-83`
- Modify: `backend/StAgent/services/session/store.py:79-141`
- Modify: `backend/tests/test_chat_message_ordering.py`
- Modify: `backend/tests/test_session_manager.py`

**Interfaces:**
- Consumes: `build_scoped_cache_key`, `get_generation`, `bump_generation`.
- Produces: chat history keys scoped by `user_id`, `session_id`, generation and query arguments.
- Preserves: database transcript as SSOT and existing public `ChatService`/`SessionManager` behavior.

- [ ] **Step 1: Add tests for stale-history elimination and failed-save ordering**

```python
# append to backend/tests/test_chat_message_ordering.py
# change the fixture setup to:
from StAgent.services.cache.service import CacheService

settings = get_settings()
cache = CacheService(settings)
svc = ChatService(settings, db_service=db, cache_service=cache)

@pytest.mark.asyncio
async def test_history_write_bumps_generation_and_old_page_is_not_reused(
    chat_svc: tuple[ChatService, UUID, UUID],
) -> None:
    service, session_id, user_id = chat_svc
    assert await service.get_history(session_id, user_id=user_id, limit=20) == []
    await service.add_message(
        session_id, MessageRole.USER, "new", user_id=user_id
    )
    history = await service.get_history(session_id, user_id=user_id, limit=20)
    assert [message.content for message in history] == ["new"]


# add to backend/tests/test_session_manager.py
@pytest.mark.asyncio
async def test_failed_database_save_does_not_publish_l1(settings_for_session) -> None:
    class FailingSessionContext:
        async def __aenter__(self):  # noqa: ANN201
            raise RuntimeError("db down")

        async def __aexit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
            return None

    class FailingDatabase:
        def session(self) -> FailingSessionContext:
            return FailingSessionContext()

    store = TieredSessionStore(settings_for_session, database=FailingDatabase())
    state = SessionState(session_id=uuid4(), user_id=uuid4())
    with pytest.raises(RuntimeError, match="db down"):
        await store.save(state)
    assert store._lru.get(state.session_id) is None
```

- [ ] **Step 2: Run the focused tests and verify failures**

Run:

```powershell
uv run pytest tests/test_chat_message_ordering.py -k "history_write_bumps" -q
uv run pytest tests/test_session_manager.py -k "failed_database_save" -q
```

Expected: the chat test exposes prefix-scan invalidation and the session test exposes cache-before-database ordering.

- [ ] **Step 3: Replace chat prefix deletion with generation keys**

```python
async def _message_cache_generation(self, session_id: UUID) -> int:
    if self._cache is None:
        return 0
    return await self._cache.get_generation(
        f"messages:{session_id}", namespace="chat"
    )

async def _invalidate_session_message_cache(self, session_id: UUID) -> None:
    if self._cache is None:
        return
    await self._cache.bump_generation(
        f"messages:{session_id}", namespace="chat"
    )
    await self._cache.delete(f"session:{session_id}", namespace="chat")
```

Add `user_id: UUID | None = None` to the keyword-only `get_history` signature. Before reading cache, load the session with `get_session(session_id, user_id=user_id)`; return `[]` when it is absent. Use `session.user_id` as the owner scope, then build:

```python
generation = await self._message_cache_generation(session_id)
cache_key = build_scoped_cache_key(
    "messages",
    owner_id=session.user_id,
    resource_id=session_id,
    generation=generation,
    vary={
        "limit": limit,
        "before_id": before_id,
        "include_system": include_system,
    },
)
```

Update `get_context_messages` to accept `user_id: UUID | None = None` and pass it to `get_history`. The only current production caller is internal, so no API router signature changes are required. Never derive owner scope from untrusted query text.

- [ ] **Step 4: Remove the unused SessionManager cache parameter**

Change constructors to:

```python
class SessionManager:
    def __init__(
        self,
        settings: Settings,
        *,
        database: DatabaseService | None,
        file_service: Any | None = None,
    ) -> None:
        self._store = TieredSessionStore(settings, database=database)


class TieredSessionStore:
    def __init__(self, settings: Settings, *, database: DatabaseService | None) -> None:
        self._settings = settings
        self._database = database
        self._lru = _LRUCache(settings.session.in_memory_lru_size)
```

Update `ServiceManager` and tests to stop passing `cache=None`.

- [ ] **Step 5: Reuse `BoundedLRU` and publish only after durable save**

```python
class _LRUCache:
    def __init__(self, max_size: int) -> None:
        self._lru = BoundedLRU(max(8, max_size))

    def get(self, session_id: UUID) -> SessionState | None:
        value, hit = self._lru.get(str(session_id))
        return value if hit else None

    def put(self, state: SessionState) -> None:
        self._lru.set(str(state.session_id), state)

    def drop(self, session_id: UUID) -> None:
        self._lru.delete(str(session_id))

async def save(self, state: SessionState) -> None:
    await self._save_to_database(state)
    self._lru.put(state)
```

Change `_save_to_database` to re-raise database failures after logging so `save` cannot publish an undurable state. In `SessionManager.save`, catch the exception only at the existing runtime recovery boundary, log `session_state_save_failed`, and leave the previously durable state untouched.

- [ ] **Step 6: Run chat and session regression suites**

Run:

```powershell
uv run pytest tests/test_session_manager.py tests/test_chat_message_ordering.py tests/test_dual_writer_dedup.py tests/test_chat_api_services.py -q
```

Expected: all tests PASS; no transcript write path uses `delete_prefix("messages:")`.

- [ ] **Step 7: Commit the state-cache correction**

```powershell
git add backend/StAgent/services/chat/service.py backend/StAgent/services/session/manager.py backend/StAgent/services/session/store.py backend/StAgent/services/service_manager.py backend/tests/test_chat_message_ordering.py backend/tests/test_session_manager.py
git commit -m "fix(cache): version chat history and preserve session durability"
```

---

### Task 6: Bound context, skill and web caches under one primitive

**Files:**
- Modify: `backend/StAgent/context/cache.py`
- Modify: `backend/StAgent/context/manager.py:121,554-640`
- Modify: `backend/StAgent/skills/resource_cache.py`
- Modify: `backend/StAgent/skills/bundle_payload_cache.py`
- Modify: `backend/StAgent/tools/web/web_search/cache.py`
- Modify: `backend/tests/test_context/test_cache.py`
- Create or modify: cache tests next to skill and web-search tests.

**Interfaces:**
- Produces: `SourceCache(max_size=256, process_ttl_sec=300.0, session_ttl_sec=900.0)`.
- Preserves: deep-copy semantics for skill cache payloads.
- Preserves: `TtlCache.get/set/clear` public behavior for Web tools.

- [ ] **Step 1: Add bounded and TTL tests for SourceCache**

```python
def test_source_cache_is_bounded() -> None:
    cache = SourceCache(max_size=2)
    cache.put("a", _block("a"), ContextScope.SESSION)
    cache.put("b", _block("b"), ContextScope.SESSION)
    cache.put("c", _block("c"), ContextScope.SESSION)
    assert cache.stats["size"] == 2
    assert cache.get("a", ContextScope.SESSION) is None


def test_turn_scope_is_never_inserted() -> None:
    cache = SourceCache()
    cache.put("k", _block(), ContextScope.TURN)
    assert cache.stats["size"] == 0
```

- [ ] **Step 2: Run context cache tests and verify failures**

Run:

```powershell
uv run pytest tests/test_context/test_cache.py -q
```

Expected: FAIL because `SourceCache` has no bound and `put` lacks scope.

- [ ] **Step 3: Reimplement SourceCache with BoundedLRU**

```python
class SourceCache:
    def __init__(
        self,
        *,
        max_size: int = 256,
        process_ttl_sec: float = 300.0,
        session_ttl_sec: float = 900.0,
    ) -> None:
        self._lru = BoundedLRU(max_size)
        self._ttls = {
            ContextScope.PROCESS: process_ttl_sec,
            ContextScope.SESSION: session_ttl_sec,
        }

    @staticmethod
    def _key(key: str, scope: ContextScope) -> str:
        return f"{scope.value}:{key}"

    def get(self, key: str, scope: ContextScope) -> ContextBlock | None:
        if scope is ContextScope.TURN:
            record_cache_event("context_source", hit=False, entries=self._lru.size)
            return None
        entry, hit = self._lru.get(self._key(key, scope))
        record_cache_event("context_source", hit=hit, entries=self._lru.size)
        if not hit:
            return None
        entry.hit_count += 1
        return entry.block

    def put(self, key: str, block: ContextBlock, scope: ContextScope) -> None:
        if scope is ContextScope.TURN:
            return
        evicted = self._lru.set(
            self._key(key, scope),
            CacheEntry(block=block, created_at=time.monotonic()),
            ttl=self._ttls[scope],
        )
        record_cache_set("context_source", evicted=evicted)
```

Update `ContextManager` to call `put(inv_key, block, source.scope)` and remove its direct Prometheus cache hit/miss calls so events are not double-counted.

- [ ] **Step 4: Move Web `TtlCache` onto BoundedLRU**

```python
class TtlCache(Generic[T]):
    def __init__(self, *, default_ttl_sec: float = 900.0, max_entries: int = 256) -> None:
        self._default_ttl = max(0.0, float(default_ttl_sec))
        self._lru = BoundedLRU(max_entries)

    def get(self, key: str) -> T | None:
        value, hit = self._lru.get(key)
        record_cache_event("web_tool", hit=hit, entries=self._lru.size)
        return value if hit else None

    def set(self, key: str, value: T, *, ttl_sec: float | None = None) -> None:
        ttl = self._default_ttl if ttl_sec is None else float(ttl_sec)
        evicted = self._lru.set(key, value, ttl=ttl)
        record_cache_set("web_tool", evicted=evicted)

    def clear(self) -> None:
        self._lru.clear()
```

Keep search and fetch as separate singleton instances and names (`web_search`, `web_fetch`) so metrics remain actionable.

- [ ] **Step 5: Make skill caches thread-safe and observable**

Replace each module-level `OrderedDict` with `BoundedLRU` and hash tuple keys to stable strings. Keep `deepcopy` on both read and write:

```python
_resource_payload_cache = BoundedLRU(_CACHE_MAX)

def _key_digest(key: tuple[Any, ...]) -> str:
    return hashlib.sha256(repr(key).encode("utf-8")).hexdigest()

def get_cached_resource_payload(key: tuple[str, int | float, int]) -> dict[str, Any] | None:
    value, hit = _resource_payload_cache.get(_key_digest(key))
    record_cache_event("skill_resource", hit=hit, entries=_resource_payload_cache.size)
    return deepcopy(value) if hit else None
```

For `put_cached_resource_payload`, call `_resource_payload_cache.set(_key_digest(key), deepcopy(payload))` and pass the returned eviction count to `record_cache_set("skill_resource", evicted=evicted)`. For the bundle cache, create `_bundle_cache = BoundedLRU(_MAX_ENTRIES)`, use `_key_digest(cache_key)`, store `(deepcopy(body_out), deepcopy(extra))`, return deep copies on hit, and report under `skill_bundle`. Both clear functions call the primitive's `clear()`.

- [ ] **Step 6: Run context, skill and web cache tests**

Run:

```powershell
uv run pytest tests/test_context/test_cache.py tests/test_skills.py tests/test_web_search.py -q
```

Expected: `tests/test_skills.py` and `tests/test_web_search.py` PASS with the context cache tests.

- [ ] **Step 7: Commit the cache consolidation**

```powershell
git add backend/StAgent/context/cache.py backend/StAgent/context/manager.py backend/StAgent/skills/resource_cache.py backend/StAgent/skills/bundle_payload_cache.py backend/StAgent/tools/web/web_search/cache.py backend/tests/test_context/test_cache.py
git commit -m "refactor(cache): bound context skill and web caches"
```

---

### Task 7: Cache embeddings and recall through real runtime wiring

**Files:**
- Delete: `backend/StAgent/memory/cache.py`
- Modify: `backend/StAgent/memory/embeddings.py`
- Modify: `backend/StAgent/memory/recall.py`
- Modify: `backend/StAgent/memory/agent_memory.py`
- Modify: `backend/StAgent/services/service_manager.py:624-675`
- Modify: `backend/tests/test_cache_primitives.py`
- Modify: `backend/tests/test_agent_memory.py`
- Modify: `backend/tests/test_memory_vector.py`
- Create: `backend/tests/test_cache_integration.py`

**Interfaces:**
- Changes: `LLMServiceEmbeddingProvider(..., cache: CacheService | None = None)`.
- Changes: `RetrievalPipeline(..., cache: CacheService | None = None)`.
- Changes: `AgentMemory(..., cache: CacheService | None = None)`.
- Produces: `AgentMemory._memory_generation_subject(owner_id, workspace_id) -> str`, `AgentMemory._recall_generation(options) -> int`, bumped after successful writes.

- [ ] **Step 1: Add embedding coalescing and owner-isolated recall tests**

```python
# backend/tests/test_cache_integration.py
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from uuid import UUID

import pytest

from StAgent.config.settings import Settings
from StAgent.memory.embeddings import LLMServiceEmbeddingProvider, NullEmbeddingProvider
from StAgent.memory.recall import RecallOptions, RetrievalPipeline
from StAgent.services.cache.service import CacheService


class FakeLLM:
    def __init__(self) -> None:
        self.embed_calls = 0

    async def embed(self, texts, *, model=None):  # noqa: ANN001
        self.embed_calls += 1
        await asyncio.sleep(0.01)
        return SimpleNamespace(embeddings=[[1.0, 0.0, 0.0] for _ in texts])


class DisabledCollection:
    can_search = False


class CountingStore:
    def __init__(self) -> None:
        self.collection = DisabledCollection()
        self.search_calls = 0

    async def lexical_search(self, query, **kwargs):  # noqa: ANN001
        self.search_calls += 1
        return []


@pytest.mark.asyncio
async def test_embedding_provider_reuses_identical_model_text() -> None:
    cache_service = CacheService(Settings())
    fake_llm = FakeLLM()
    provider = LLMServiceEmbeddingProvider(
        fake_llm, model="embed-v1", dimension=3, cache=cache_service
    )
    first, second = await asyncio.gather(
        provider.embed_one("same"), provider.embed_one("same")
    )
    assert first == second
    assert fake_llm.embed_calls == 1


@pytest.mark.asyncio
async def test_recall_key_isolated_by_user() -> None:
    cache_service = CacheService(Settings())
    episodic = CountingStore()
    semantic = CountingStore()
    procedural = CountingStore()
    pipeline = RetrievalPipeline(
        episodic=episodic,
        semantic=semantic,
        procedural=procedural,
        embeddings=NullEmbeddingProvider(dimension=3),
        cache=cache_service,
    )
    await pipeline.recall(RecallOptions(query="q", user_id=UUID(int=1)))
    await pipeline.recall(RecallOptions(query="q", user_id=UUID(int=2)))
    assert episodic.search_calls == 2
```

- [ ] **Step 2: Run new integration tests and verify constructor failures**

Run:

```powershell
uv run pytest tests/test_cache_integration.py -q
```

Expected: FAIL because memory constructors do not accept `cache`.

- [ ] **Step 3: Replace the private cachetools Embedding LRU**

```python
class LLMServiceEmbeddingProvider:
    def __init__(
        self,
        llm_service: "LLMService",
        *,
        cache: "CacheService | None" = None,
        model: str | None = None,
        dimension: int = 1024,
    ) -> None:
        self._llm = llm_service
        self._cache = cache
        self._model = model
        self.dimension = dimension
        self._batch_lock = asyncio.Lock()

    async def _embed_uncached(self, texts: list[str]) -> list[list[float]]:
        try:
            response = await self._llm.embed(texts, model=self._model)
        except Exception as exc:  # noqa: BLE001
            self.last_degraded = True
            self.last_error = str(exc)
            raise RuntimeError(f"embedding_backend_failed: {exc}") from exc
        vectors = list(response.embeddings or [])
        if len(vectors) != len(texts):
            self.last_degraded = True
            self.last_error = "embedding_response_incomplete"
            raise RuntimeError("embedding_response_incomplete")
        if vectors and len(vectors[0]) != self.dimension:
            self.dimension = len(vectors[0])
        self.last_degraded = False
        self.last_error = None
        return vectors

    async def embed(self, texts: Iterable[str]) -> list[list[float]]:
        items = [text or "" for text in texts]
        if not items:
            return []
        if self._cache is None:
            return await self._embed_uncached(items)
        keys = [
            build_scoped_cache_key(
                "embedding",
                resource_id=self.model,
                vary={"text": text, "dimension": self.dimension},
            )
            for text in items
        ]
        cached = await self._cache.get_many(keys, namespace="embeddings")
        async with self._batch_lock:
            missing_keys = [key for key in dict.fromkeys(keys) if key not in cached]
            refreshed = await self._cache.get_many(
                missing_keys, namespace="embeddings"
            )
            cached.update(refreshed)
            missing_keys = [key for key in missing_keys if key not in cached]
            if missing_keys:
                text_for_key = dict(zip(keys, items, strict=True))
                vectors = await self._embed_uncached(
                    [text_for_key[key] for key in missing_keys]
                )
                writes = dict(zip(missing_keys, vectors, strict=True))
                await self._cache.set_many(writes, namespace="embeddings")
                cached.update(writes)
        return [list(cached[key]) for key in keys]

    async def embed_one(self, text: str) -> list[float]:
        result = await self.embed([text])
        if not result:
            raise RuntimeError("embedding_empty")
        return result[0]
```

Remove the `cachetools.LRUCache` import and `_CACHE_MAX_SIZE`. Never log text or vector values.

- [ ] **Step 4: Add generation-aware Recall caching**

Add `cache` and `generation_getter` to `RetrievalPipeline`. At the start of `recall`, build a key from user, workspace, session, generation, query and all result-shaping options. Cache a `deepcopy(RecallBundle)` for the `recall` namespace TTL; on hit return a deep copy so `note_recall` or caller mutation cannot modify resident state.

```python
generation = await self._generation_getter(options) if self._generation_getter else 0
key = build_scoped_cache_key(
    "recall",
    owner_id=options.user_id,
    resource_id=options.workspace_id or options.session_id,
    generation=generation,
    vary={
        "query": query,
        "limit": options.limit,
        "per_store_limit": options.per_store_limit,
        "episodic": options.include_episodic,
        "semantic": options.include_semantic,
        "procedural": options.include_procedural,
        "bm25": options.bm25_rerank,
    },
)
```

Do not cache when `file_state` contains current-turn exclusions unless its deterministic signature is included in `vary`.

- [ ] **Step 5: Bump memory generation only after successful writes**

```python
@staticmethod
def _memory_generation_subject(
    user_id: UUID | None, workspace_id: UUID | None
) -> str:
    return f"{user_id or '-'}:{workspace_id or '-'}"

async def _recall_generation(self, options: RecallOptions) -> int:
    if self._cache is None:
        return 0
    return await self._cache.get_generation(
        self._memory_generation_subject(options.user_id, options.workspace_id),
        namespace="memory",
    )

async def _bump_memory_generation(
    self, user_id: UUID | None, workspace_id: UUID | None
) -> None:
    if self._cache is None:
        return
    await self._cache.bump_generation(
        self._memory_generation_subject(user_id, workspace_id),
        namespace="memory",
    )
```

Construct `RetrievalPipeline(..., cache=cache, generation_getter=self._recall_generation)`. Call `_bump_memory_generation` after successful `episodic.record`, `semantic.upsert`, and `procedural.record`; never call it inside failure branches. Pass the same cache to `LLMServiceEmbeddingProvider` and `AgentMemory` from `ServiceManager._start_agent_memory`.

- [ ] **Step 6: Remove the unused parallel memory cache layer**

After migrating imports, run:

```powershell
rg -n "from StAgent.memory.cache|EmbeddingCache|RecallCache|MemoryCache" backend/StAgent backend/tests
```

Expected before deletion: production code has zero matches; only `backend/StAgent/memory/cache.py` and the `MemoryCache` section in `backend/tests/test_cache_primitives.py` match. Delete `backend/StAgent/memory/cache.py` and remove the `_FlakyL2` plus `MemoryCache`-only tests and imports from `backend/tests/test_cache_primitives.py`. The production Embedding and Recall paths above are the only memory result caches after this step.

- [ ] **Step 7: Run memory and integration suites**

Run:

```powershell
uv run pytest tests/test_cache_integration.py tests/test_agent_memory.py tests/test_memory_vector.py tests/test_memory_formation.py -q
```

Expected: all tests PASS; identical concurrent embedding requests produce one provider call; memory write failure does not bump generation.

- [ ] **Step 8: Commit runtime memory caching**

```powershell
git add backend/StAgent/memory/cache.py backend/StAgent/memory/embeddings.py backend/StAgent/memory/recall.py backend/StAgent/memory/agent_memory.py backend/StAgent/services/service_manager.py backend/tests/test_cache_primitives.py backend/tests/test_cache_integration.py backend/tests/test_agent_memory.py backend/tests/test_memory_vector.py
git commit -m "feat(memory): cache embeddings and version recall results"
```

---

### Task 8: Close workflow cache isolation and capacity gaps

**Files:**
- Modify: `backend/StAgent/workflow/engine/caching.py:35-390`
- Modify: `backend/StAgent/workflow/engine/executor.py:78-110`
- Modify: `backend/StAgent/workflow/engine/runner.py:180-280`
- Modify: `backend/StAgent/services/service_manager.py:481-490`
- Modify: `backend/StAgent/workflow/cli/workflow_worker.py:102-112`
- Modify: `backend/StAgent/workflow/nodes/builtin/subworkflow.py:65-80`
- Modify: `backend/tests/workflow/test_engine.py`
- Modify: `backend/tests/workflow/test_execution_list.py`

**Interfaces:**
- Produces: `WorkflowCacheScope(owner_id, workflow_digest, permission_digest, model_revision)`.
- Changes: `WorkflowExecutor(..., cache_mode="lru", cache_options=None, cache_scope=None)`.
- Preserves: deterministic output caching and `NOT_CACHEABLE` behavior.

- [ ] **Step 1: Add direct-constructor and cross-scope tests**

```python
def test_executor_default_output_cache_is_bounded() -> None:
    executor = WorkflowExecutor()
    assert isinstance(executor.cache_set.outputs, LRUCache)
    assert executor.cache_set.outputs.max_size == 128
    assert isinstance(executor.cache_set.objects, LRUCache)
    assert isinstance(executor.cache_set.ui, LRUCache)


def test_identical_node_inputs_do_not_cross_owner_scope() -> None:
    prompt = {
        "end": {"class_type": "EndNode", "inputs": {"result": "same"}}
    }
    left = CacheKeySetInputSignature(
        prompt, scope_digest=WorkflowCacheScope(owner_id="u1").digest()
    )
    right = CacheKeySetInputSignature(
        prompt, scope_digest=WorkflowCacheScope(owner_id="u2").digest()
    )
    assert left.signature("end") != right.signature("end")
```

- [ ] **Step 2: Run workflow tests and verify failures**

Run:

```powershell
uv run pytest tests/workflow/test_engine.py -k "bounded or owner_scope" -q
```

Expected: direct constructor uses `classic`/`BasicCache`, and cache keys do not contain scope.

- [ ] **Step 3: Make every default bounded and pass configured sizes**

```python
def build_cache_set(mode: str = "lru", **options: Any) -> CacheSet:
    mode = (mode or "lru").lower()
    if mode not in {"lru", "ram", "none", "hierarchical"}:
        raise ValueError(f"unsupported workflow cache mode: {mode}")
    lru_size = max(1, int(options.get("lru_size", 128)))
    if mode == "none":
        outputs: BaseCache = NullCache()
    elif mode == "lru":
        outputs = LRUCache(max_size=lru_size)
    elif mode == "ram":
        outputs = RAMPressureCache(
            ram_budget_mb=max(1, int(options.get("ram_mb", 256)))
        )
    else:
        factory_name = options.get("factory", "lru")
        if factory_name == "ram":
            ram_mb = max(1, int(options.get("ram_mb", 256)))
            factory = lambda: RAMPressureCache(ram_budget_mb=ram_mb)  # noqa: E731
        else:
            factory = lambda: LRUCache(max_size=lru_size)  # noqa: E731
        outputs = HierarchicalCache(factory)
    return CacheSet(
        outputs=outputs,
        objects=LRUCache(max_size=lru_size),
        ui=LRUCache(max_size=lru_size),
    )
```

Change `WorkflowExecutor.cache_mode` default to `"lru"` and add `cache_options: dict[str, Any] | None`. Construct with `build_cache_set(cache_mode, **(cache_options or {}))`.

In ServiceManager and CLI worker pass:

```python
cache_options={
    "lru_size": wf_settings.cache_lru_size,
    "ram_mb": wf_settings.cache_ram_mb,
}
```

Subworkflow nodes must inherit parent executor cache mode/options or explicitly use `lru`; they must not silently return to `classic`.

- [ ] **Step 4: Prefix node signatures with execution scope**

```python
@dataclass(frozen=True, slots=True)
class WorkflowCacheScope:
    owner_id: str = "local"
    workflow_digest: str = "adhoc"
    permission_digest: str = "default"
    model_revision: str = "default"

    def digest(self) -> str:
        return hash_signature(asdict(self))
```

Pass the scope digest to `CacheKeySetInputSignature`; return `f"{scope_digest}:{node_signature}"`. Pass the same digest to `NodeRunner` and use `f"{scope_digest}:{node_id}"` for object-cache keys. Build owner and permission scope at ingress from authenticated user/workspace and the allowed-tool set. Internal local callers use `owner_id="local"`, never a shared empty string.

- [ ] **Step 5: Account for output size and reject oversized workflow payloads**

Populate `CacheEntry.size_bytes` using `_estimate_size(output)` before `output_cache.set`. `RAMPressureCache` then enforces its byte budget. For LRU mode, skip caching when size exceeds `settings.cache.max_value_bytes`; still return the node result normally and increment `workflow_node_output` rejection statistics.

- [ ] **Step 6: Run workflow engine and resume suites**

Run:

```powershell
uv run pytest tests/workflow/test_engine.py tests/workflow/test_execution_list.py tests/workflow/test_executor_resume.py tests/test_workflow_state_store.py -q
```

Expected: all tests PASS; direct constructors are bounded; owner scopes never share outputs; pause/resume cache snapshots still restore supported entries.

- [ ] **Step 7: Commit workflow cache isolation**

```powershell
git add backend/StAgent/workflow/engine/caching.py backend/StAgent/workflow/engine/executor.py backend/StAgent/workflow/engine/runner.py backend/StAgent/services/service_manager.py backend/StAgent/workflow/cli/workflow_worker.py backend/StAgent/workflow/nodes/builtin/subworkflow.py backend/tests/workflow/test_engine.py backend/tests/workflow/test_execution_list.py
git commit -m "fix(workflow): bound and isolate node output caches"
```

---

### Task 9: Finish observability and safe runtime controls

**Files:**
- Modify: `backend/StAgent/services/cache/observability.py`
- Modify: `backend/StAgent/utils/metrics.py`
- Modify: `backend/StAgent/api/v1/stats.py:54-91,482-545`
- Modify: `backend/tests/test_cache_optimization.py`
- Create: `backend/tests/test_stats_api.py`
- Create: `backend/tests/test_cache_metrics.py`

**Interfaces:**
- Produces per cache: hits, misses, recent hit rate, sets, LRU evictions, expirations, rejected writes, resident items, max items, fill count, coalesced waiters.
- Preserves low-cardinality labels: only logical cache name is exported to Prometheus.
- Runtime config remains process-local and explicitly reports `persistent: false`.

- [ ] **Step 1: Add snapshot and API contract tests**

```python
def test_cache_snapshot_exposes_pressure_and_fill_metrics() -> None:
    reg = CacheStatsRegistry()
    reg.record_rejection("chat")
    reg.record_fill("chat", coalesced=9)
    cache = reg.snapshot()["caches"][0]
    assert cache["rejections"] == 1
    assert cache["fills"] == 1
    assert cache["coalesced"] == 9


# backend/tests/test_stats_api.py
from fastapi.testclient import TestClient


def test_cache_config_response_is_explicitly_ephemeral(
    client: TestClient,
) -> None:
    response = client.get("/api/v1/stats/cache")
    assert response.status_code == 200
    assert response.json()["config"]["persistent"] is False


# backend/tests/test_cache_metrics.py
from prometheus_client import CollectorRegistry

from StAgent.utils.metrics import StAgentMetrics


def test_cache_pressure_metrics_use_only_cache_name_label() -> None:
    metrics = StAgentMetrics(CollectorRegistry())
    metrics.record_cache_rejection("chat")
    metrics.record_cache_fill("chat", duration_sec=0.01, coalesced=4)
    payload = metrics.generate_metrics().decode("utf-8")
    assert 'cache_name="chat"' in payload
    assert "owner_id" not in payload
    assert "resource_id" not in payload
```

- [ ] **Step 2: Run tests and verify response-model failures**

Run:

```powershell
uv run pytest tests/test_cache_optimization.py tests/test_stats_api.py -q
```

Expected: FAIL on missing fields.

- [ ] **Step 3: Extend registry counters without provider callbacks under lock**

Add `rejections`, `fills`, `coalesced` fields and record methods. Change `snapshot()` to copy stat counters and provider callables while holding the registry lock, release the lock, then call `size_provider/detail_provider`. This prevents slow providers and future lock inversions from blocking cache event recording.

```python
def snapshot(self) -> dict[str, Any]:
    with self._lock:
        rows = [stat.copy_for_snapshot() for stat in self._stats.values()]
    caches = [row.snapshot() for row in sorted(rows, key=lambda item: item.name)]
    # aggregate from copied rows and return
```

`copy_for_snapshot` must copy `_window` bucket lists, not share mutable lists.

- [ ] **Step 4: Extend Prometheus metrics with low-cardinality counters**

Add counters/gauges for eviction, expiration, rejection, fill duration and coalesced waiters. Labels are limited to `cache_name`; never export key, owner, session, model name or rejection detail. Measure fill time around `_fill` with `time.perf_counter()`.

- [ ] **Step 5: Make runtime configuration validation explicit**

Extend `config_snapshot()`:

```python
return {
    "backend": "memory",
    "persistent": False,
    "default_ttl": cfg.default_ttl,
    "max_items": cfg.max_items,
    "max_value_bytes": cfg.max_value_bytes,
    "shard_count": cfg.shard_count,
    "namespace_ttls": dict(cfg.namespace_ttls),
    "resident_items": self._memory_cache.size,
}
```

In `update_limits`, reject `max_items < shard_count` with `ValueError`; translate it to HTTP 422. Change the mutation endpoint parameter from `user_id: CurrentUserId` to `_admin_id: AdminUserId`, imported from `StAgent.services.auth.deps`. Keep the read-only stats endpoint on `CurrentUserId`.

- [ ] **Step 6: Run observability and API suites**

Run:

```powershell
uv run pytest tests/test_cache_optimization.py tests/test_stats_api.py tests/test_cache_metrics.py -q
```

Expected: all tests PASS; `/stats/cache` works after real `ServiceManager` startup and reports `backend=memory`, `persistent=false`.

- [ ] **Step 7: Commit observability and controls**

```powershell
git add backend/StAgent/services/cache/observability.py backend/StAgent/services/cache/service.py backend/StAgent/utils/metrics.py backend/StAgent/api/v1/stats.py backend/tests/test_cache_optimization.py backend/tests/test_stats_api.py
git commit -m "feat(cache): expose pressure and fill observability"
```

---

### Task 10: Add benchmarks, deployment knobs and operational documentation

**Files:**
- Create: `backend/tests/bench_cache_hot_paths.py`
- Create: `docs/technical/cache-architecture.md`
- Modify: `deploy/.env.example`
- Modify: `docs/guides/agent/18-prompt-cache-and-context-hygiene.md`

**Interfaces:**
- Produces repeatable benchmark JSON for cold, hot and invalidated paths.
- Documents exact Redis promotion gate without claiming Redis exists.

- [ ] **Step 1: Add a benchmark script with machine-readable output**

```python
# backend/tests/bench_cache_hot_paths.py
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time

from StAgent.config.settings import Settings
from StAgent.services.cache.service import CacheService


async def _measure(iterations: int) -> dict[str, float | int]:
    service = CacheService(Settings())
    await service.start()
    timings: list[float] = []
    calls = 0

    async def factory() -> dict[str, int]:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.005)
        return {"value": 1}

    for _ in range(iterations):
        started = time.perf_counter()
        await service.get_or_set("hot", factory, namespace="chat")
        timings.append((time.perf_counter() - started) * 1_000)
    await service.stop()
    ordered = sorted(timings)
    return {
        "iterations": iterations,
        "factory_calls": calls,
        "p50_ms": statistics.median(ordered),
        "p95_ms": ordered[max(0, int(len(ordered) * 0.95) - 1)],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=1_000)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(_measure(args.iterations)), sort_keys=True))
```

- [ ] **Step 2: Run the benchmark and save evidence outside version control**

Run:

```powershell
uv run python tests/bench_cache_hot_paths.py --iterations 1000
```

Expected JSON invariants: `iterations=1000`, `factory_calls=1`, finite positive p50/p95. Do not commit machine-specific latency numbers as universal targets.

- [ ] **Step 3: Document runtime semantics**

`docs/technical/cache-architecture.md` must contain these concrete sections:

1. Cache ownership table: CacheService, session L1, workflow output cache, provider prompt cache.
2. Read path: L1 lookup → database/provider fill → bounded write.
3. Write path: durable commit → generation bump → later TTL cleanup.
4. Key schema with owner/resource/generation/variant examples.
5. TTL semantics: `None`, zero/negative, jitter, maintenance sweep.
6. Values that must never be cached.
7. Metrics and alert meanings.
8. Failure behavior and rollback.
9. Redis promotion gate: PostgreSQL + multi-worker + measured cross-worker misses.
10. Proof boundary: unit tests do not prove production hit rate.

Update the prompt-cache guide to distinguish provider prompt-prefix caching from application result caching; neither is an ACL.

- [ ] **Step 4: Add annotated environment settings**

```dotenv
# Process-local application cache (no Redis required)
StAgent_CACHE_DEFAULT_TTL=3600
StAgent_CACHE_MAX_ITEMS=10000
StAgent_CACHE_MAX_VALUE_BYTES=1048576
StAgent_CACHE_SHARD_COUNT=1
StAgent_CACHE_MAINTENANCE_INTERVAL_SEC=30
StAgent_CACHE_TTL_JITTER_RATIO=0.05
# JSON map; mutable recall is short, stable embeddings are longer-lived.
StAgent_CACHE_NAMESPACE_TTLS={"embeddings":7200,"recall":300,"files":1800,"chat":900,"memory":1800,"webhooks":600,"variables":1800}
```

- [ ] **Step 5: Commit docs and benchmark tooling**

```powershell
git add backend/tests/bench_cache_hot_paths.py docs/technical/cache-architecture.md docs/guides/agent/18-prompt-cache-and-context-hygiene.md deploy/.env.example
git commit -m "docs(cache): add operations guide and hot-path benchmark"
```

---

### Task 11: Run the final confidence loop and prepare staged rollout

**Files:**
- Modify only files needed to fix failures found by this task.

**Interfaces:**
- Consumes every preceding task.
- Produces verification evidence and a rollback-ready change set.

- [ ] **Step 1: Run formatting and static checks on changed backend files**

Run:

```powershell
uv run ruff check StAgent/services/cache StAgent/services/service_manager.py StAgent/services/chat/service.py StAgent/services/session StAgent/context StAgent/skills StAgent/tools/web/web_search/cache.py StAgent/memory StAgent/workflow/engine tests/test_cache_keys.py tests/test_cache_singleflight.py tests/test_cache_service_manager.py tests/test_cache_integration.py
uv run black --check StAgent/services/cache StAgent/services/service_manager.py StAgent/services/chat/service.py StAgent/services/session StAgent/context StAgent/skills StAgent/tools/web/web_search/cache.py StAgent/memory StAgent/workflow/engine tests/test_cache_keys.py tests/test_cache_singleflight.py tests/test_cache_service_manager.py tests/test_cache_integration.py
```

Expected: exit code 0 for both commands. Fix only reported files and rerun until clean.

- [ ] **Step 2: Run the complete cache and affected-domain suite**

Run:

```powershell
uv run pytest tests/test_cache_keys.py tests/test_cache_singleflight.py tests/test_cache_service_manager.py tests/test_cache_integration.py tests/test_cache_optimization.py tests/test_cache_primitives.py tests/test_services.py tests/test_context tests/test_session_manager.py tests/test_agent_memory.py tests/test_memory_vector.py tests/workflow -q
```

Expected: zero failures and zero errors.

- [ ] **Step 3: Run the full backend suite**

Run:

```powershell
uv run pytest tests/ -q
```

Expected: zero failures. If unrelated pre-existing failures occur, record their exact test names and reproduce them against the pre-change revision before classifying them as unrelated.

- [ ] **Step 4: Run concurrency and pressure verification**

Run the benchmark from Task 10 and an integration loop that performs at least:

- 100 concurrent identical `get_or_set` calls: one factory call.
- 100 concurrent generation bumps: final generation 100.
- 20,000 unique writes with `max_items=1,000`: resident items never exceed 1,000.
- An oversized value: request succeeds without caching, rejection counter increments.
- A database write failure: no new cache entry becomes visible.
- Two owner IDs with identical inputs: no shared workflow/recall hit.

Expected: every invariant holds. Latency is reported as local evidence, not a universal claim.

- [ ] **Step 5: Perform a local API smoke test**

Start the backend in a separate PowerShell terminal with:

```powershell
$env:StAgent_PORT='7860'
uv run python -m StAgent.server
```

Then verify:

```powershell
Invoke-RestMethod http://127.0.0.1:7860/api/v1/health
Invoke-RestMethod http://127.0.0.1:7860/api/v1/stats/cache
```

Expected: health includes a ready cache service. Obtain the local access token through the existing test/local-login flow, send it as `Authorization: Bearer <token>` to `/api/v1/stats/cache`, and verify `backend=memory`, `persistent=false`, bounded capacity and non-negative counters. Do not weaken authentication for the smoke test.

- [ ] **Step 6: Review the diff for hidden scope expansion**

Run:

```powershell
git diff --check
git status --short
git diff -- backend/StAgent/services/cache backend/StAgent/services/service_manager.py backend/StAgent/services/chat/service.py backend/StAgent/services/session backend/StAgent/context backend/StAgent/skills backend/StAgent/tools/web/web_search/cache.py backend/StAgent/memory backend/StAgent/workflow/engine backend/StAgent/api/v1/stats.py deploy/.env.example docs/technical/cache-architecture.md
```

Expected: no whitespace errors; no secret values; no Redis dependency; no unrelated user files staged.

- [ ] **Step 7: Apply staged rollout gates**

Rollout sequence:

1. Enable only lifecycle wiring and bounded LRU in local development.
2. Verify chat/session invalidation for one full manual conversation including edit, delete and resume.
3. Enable Embedding and Recall caching; compare provider-call counts and recall results with cache disabled.
4. Enable workflow scope keys; rerun saved workflows and pause/resume.
5. Keep `shard_count=1` until profiling shows lock contention; then test 4 shards with `max_items>=4`.
6. Roll back by setting workflow mode to `none` and namespace TTLs to short values; if correctness is questioned, stop wiring domain consumers while keeping DB reads intact.

Release gate: no stale read after a successful mutation, no cache publication after failed durability, no cross-owner hit, bounded resident set, full affected test suite green.

- [ ] **Step 8: Commit final verification-only fixes**

If Step 1-7 required code corrections, explicitly stage only those corrections and commit:

```powershell
git commit -m "test(cache): close integration and rollout gaps"
```

If no corrections were required, do not create an empty commit.

## Acceptance Matrix

| Requirement | Evidence |
|---|---|
| Real runtime uses cache | `ServiceManager.cache` ready; Chat/File/Variable receive the same instance |
| Database remains SSOT | failed save never publishes L1; cache can be cleared without data loss |
| No stampede | 50-100 concurrent identical fills execute one factory |
| No lost increments | concurrent generation test ends at exact expected value |
| No tenant leakage | owner-isolation tests for chat, recall and workflow |
| Bounded memory | `max_items` invariant and oversized rejection test |
| Correct invalidation | chat/memory generation changes after successful writes only |
| Workflow safety | direct executor defaults to bounded LRU; side-effect nodes remain uncacheable |
| Observable behavior | recent/cumulative hit rate, fill, coalesced, eviction, expiration, rejection and resident size |
| Failure degradation | cache failure falls back to DB/provider; business result remains correct |
| Zero-dependency default | no Redis package/service required; SQLite single-worker deployment unchanged |

## Non-goals for this plan

- 不缓存完整 LLM 对话响应；模型输出受采样参数、工具状态和安全策略影响，风险高于收益。
- 不把 Redis、PostgreSQL 或多 worker 作为默认配置。
- 不缓存权限判断、审批决定、密钥、验证码或一次性下载凭证。
- 不承诺固定命中率。命中率目标必须由真实请求分布建立基线后再设定。
- 不把前端 React Query 缓存并入后端缓存控制面；前端继续按页面数据新鲜度独立管理。
