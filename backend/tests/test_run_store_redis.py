"""RunStore seam + Redis-backed cross-worker behaviour.

These tests back Phase 5 of the optimisation plan. They exercise the pluggable
:class:`RunStore` seam and the :class:`RedisRunStore` serialization/lookup logic
against an in-memory fake redis client that simulates *two workers* sharing one
Redis (no real redis dependency required).
"""

from __future__ import annotations

from typing import Any

import pytest

from leagent.runtime import execution_factory
from leagent.runtime.execution_run import ExecutionRun, ExecutionScope, PauseToken
from leagent.runtime.redis_run_store import RedisRunStore
from leagent.runtime.run_store import (
    RunStore,
    get_run_store,
    reset_run_store,
    set_run_store,
)


class FakeRedis:
    """Minimal synchronous redis stand-in shared by simulated workers.

    Returns ``bytes`` like a default redis-py client so the store's decode path
    is exercised. TTL is tracked but not expired (tests drive lifecycle
    explicitly via ``remove``).
    """

    def __init__(self) -> None:
        self.kv: dict[str, bytes] = {}
        self.sets: dict[str, set[bytes]] = {}
        self.ttls: dict[str, int] = {}

    def set(self, key: str, value: Any, ex: int | None = None) -> None:
        self.kv[key] = value.encode() if isinstance(value, str) else bytes(value)
        if ex is not None:
            self.ttls[key] = ex

    def get(self, key: str) -> bytes | None:
        return self.kv.get(key)

    def delete(self, *keys: str) -> None:
        for key in keys:
            self.kv.pop(key, None)
            self.ttls.pop(key, None)

    def sadd(self, key: str, member: str) -> None:
        self.sets.setdefault(key, set()).add(member.encode())

    def srem(self, key: str, member: str) -> None:
        members = self.sets.get(key)
        if members is not None:
            members.discard(member.encode())

    def smembers(self, key: str) -> set[bytes]:
        return set(self.sets.get(key, set()))

    def expire(self, key: str, ttl: int) -> None:
        self.ttls[key] = ttl


@pytest.fixture(autouse=True)
def _restore_default_store() -> Any:
    yield
    reset_run_store()


# ---------------------------------------------------------------------------
# RunStore seam
# ---------------------------------------------------------------------------


def test_default_store_is_in_process_registry() -> None:
    from leagent.runtime.execution_registry import get_execution_run_registry

    assert get_run_store() is get_execution_run_registry()


def test_set_and_reset_run_store() -> None:
    fake = FakeRedis()
    store = RedisRunStore(fake)
    set_run_store(store)
    assert get_run_store() is store

    reset_run_store()
    from leagent.runtime.execution_registry import get_execution_run_registry

    assert get_run_store() is get_execution_run_registry()


def test_set_run_store_rejects_non_conforming() -> None:
    with pytest.raises(TypeError):
        set_run_store(object())  # type: ignore[arg-type]


def test_factory_uses_installed_store() -> None:
    """begin/end_execution route through whichever store is installed."""
    fake = FakeRedis()
    set_run_store(RedisRunStore(fake))

    run = execution_factory.begin_execution(
        scope=ExecutionScope.CHAT_TURN, session_id="s1", prompt_id="p1"
    )
    assert get_run_store().get(run.run_id) is not None

    execution_factory.end_execution(run.run_id)
    assert get_run_store().get(run.run_id) is None


# ---------------------------------------------------------------------------
# RedisRunStore cross-worker behaviour
# ---------------------------------------------------------------------------


def test_redis_store_conforms_to_protocol() -> None:
    assert isinstance(RedisRunStore(FakeRedis()), RunStore)


def test_cross_worker_register_and_lookup() -> None:
    shared = FakeRedis()
    worker_a: RunStore = RedisRunStore(shared)
    worker_b: RunStore = RedisRunStore(shared)

    run = ExecutionRun(
        scope=ExecutionScope.WORKFLOW,
        session_id="s1",
        prompt_id="prompt-xyz",
        metadata={"k": "v"},
    )
    worker_a.register(run)

    # Worker B sees the run registered by worker A.
    seen = worker_b.get(run.run_id)
    assert seen is not None
    assert seen.run_id == run.run_id
    assert seen.scope == ExecutionScope.WORKFLOW
    assert seen.metadata == {"k": "v"}
    assert worker_b.get_by_prompt_id("prompt-xyz").run_id == run.run_id
    assert [r.run_id for r in worker_b.list_for_session("s1")] == [run.run_id]


def test_cross_worker_pause_persists_via_reregister() -> None:
    shared = FakeRedis()
    worker_a: RunStore = RedisRunStore(shared)
    worker_b: RunStore = RedisRunStore(shared)

    run = ExecutionRun(scope=ExecutionScope.CHAT_TURN, session_id="s1")
    worker_a.register(run)

    # Worker A pauses the run and re-registers it (the pattern used by loop.py).
    fetched = worker_a.get(run.run_id)
    assert fetched is not None
    fetched.pause_token = PauseToken(scope=ExecutionScope.CHAT_TURN, reason="awaiting_user_input")
    worker_a.register(fetched)

    # Worker B observes the persisted pause.
    seen = worker_b.get(run.run_id)
    assert seen is not None
    assert seen.is_blocked is True
    assert seen.pause_token is not None
    assert seen.pause_token.reason == "awaiting_user_input"


def test_cross_worker_remove_clears_indexes() -> None:
    shared = FakeRedis()
    worker_a: RunStore = RedisRunStore(shared)
    worker_b: RunStore = RedisRunStore(shared)

    run = ExecutionRun(scope=ExecutionScope.CHAT_TURN, session_id="s1", prompt_id="p9")
    worker_a.register(run)
    worker_a.remove(run.run_id)

    assert worker_b.get(run.run_id) is None
    assert worker_b.get_by_prompt_id("p9") is None
    assert worker_b.list_for_session("s1") == []


def test_get_active_chat_turn_orders_by_wall_clock() -> None:
    shared = FakeRedis()
    store: RunStore = RedisRunStore(shared)

    older = ExecutionRun(scope=ExecutionScope.CHAT_TURN, session_id="s1", created_at=100.0)
    newer = ExecutionRun(scope=ExecutionScope.CHAT_TURN, session_id="s1", created_at=200.0)
    workflow = ExecutionRun(scope=ExecutionScope.WORKFLOW, session_id="s1", created_at=300.0)
    for run in (older, newer, workflow):
        store.register(run)

    active = store.get_active_chat_turn("s1")
    assert active is not None
    assert active.run_id == newer.run_id  # newest chat turn wins, workflow ignored


def test_remove_keeps_prompt_index_when_reused() -> None:
    shared = FakeRedis()
    store: RunStore = RedisRunStore(shared)

    old = ExecutionRun(scope=ExecutionScope.CHAT_TURN, prompt_id="dup")
    new = ExecutionRun(scope=ExecutionScope.CHAT_TURN, prompt_id="dup")
    store.register(old)
    store.register(new)  # prompt index now points at ``new``

    store.remove(old.run_id)

    resolved = store.get_by_prompt_id("dup")
    assert resolved is not None
    assert resolved.run_id == new.run_id
