"""Lifecycle guarantees for :class:`ExecutionRunRegistry`.

Covers the invariants that back Phase 3 of the optimisation plan:

* every terminal path (normal completion, exception, cancellation) retires the
  run from the in-process registry;
* paused/blocked runs are *intentionally* retained by
  ``end_execution_unless_blocked`` so a later resume can re-link them;
* abandoned handles (a pause that is never resumed) are reclaimed by the
  throttled TTL sweep so a long-running single-process server stays bounded.
"""

from __future__ import annotations

import asyncio

import pytest

from StAgent.runtime import execution_factory
from StAgent.runtime.execution_registry import ExecutionRunRegistry
from StAgent.runtime.execution_run import ExecutionRun, ExecutionScope, PauseToken
from StAgent.runtime.run_store import reset_run_store, set_run_store


@pytest.fixture
def registry() -> ExecutionRunRegistry:
    """A fresh registry installed as the process run store for the test."""
    reg = ExecutionRunRegistry()
    set_run_store(reg)
    try:
        yield reg
    finally:
        reset_run_store()


# ---------------------------------------------------------------------------
# Registry unit behaviour
# ---------------------------------------------------------------------------


def test_register_get_remove_roundtrip() -> None:
    reg = ExecutionRunRegistry()
    run = ExecutionRun(scope=ExecutionScope.CHAT_TURN, prompt_id="p1")

    reg.register(run)
    assert reg.get(run.run_id) is run
    assert reg.get_by_prompt_id("p1") is run
    assert reg.active_count() == 1

    reg.remove(run.run_id)
    assert reg.get(run.run_id) is None
    assert reg.get_by_prompt_id("p1") is None
    assert reg.active_count() == 0


def test_remove_keeps_prompt_index_when_reused() -> None:
    """A newer run reusing the same prompt_id must not lose its index entry."""
    reg = ExecutionRunRegistry()
    old = ExecutionRun(scope=ExecutionScope.CHAT_TURN, prompt_id="dup")
    new = ExecutionRun(scope=ExecutionScope.CHAT_TURN, prompt_id="dup")
    reg.register(old)
    reg.register(new)  # prompt index now points at ``new``

    reg.remove(old.run_id)

    assert reg.get_by_prompt_id("dup") is new


def test_list_and_active_chat_turn() -> None:
    reg = ExecutionRunRegistry()
    a = ExecutionRun(scope=ExecutionScope.CHAT_TURN, session_id="s", registered_at=1.0)
    b = ExecutionRun(scope=ExecutionScope.CHAT_TURN, session_id="s", registered_at=2.0)
    workflow = ExecutionRun(scope=ExecutionScope.WORKFLOW, session_id="s", registered_at=3.0)
    for run in (a, b, workflow):
        reg.register(run)

    assert {r.run_id for r in reg.list_for_session("s")} == {a.run_id, b.run_id, workflow.run_id}
    assert reg.get_active_chat_turn("s") is b  # newest chat turn wins
    assert reg.get_active_chat_turn("missing") is None


# ---------------------------------------------------------------------------
# Terminal-path guarantees via the factory
# ---------------------------------------------------------------------------


def test_normal_completion_removes_run(registry: ExecutionRunRegistry) -> None:
    run = execution_factory.begin_execution(scope=ExecutionScope.CHAT_TURN)
    assert registry.active_count() == 1

    execution_factory.end_execution(run.run_id)
    assert registry.active_count() == 0


def test_exception_path_removes_run(registry: ExecutionRunRegistry) -> None:
    """A caller wrapping the run in try/finally must leave nothing behind."""
    run = execution_factory.begin_execution(scope=ExecutionScope.TASK)
    with pytest.raises(RuntimeError):
        try:
            raise RuntimeError("boom")
        finally:
            execution_factory.end_execution(run.run_id)
    assert registry.active_count() == 0


def test_cancellation_path_removes_run(registry: ExecutionRunRegistry) -> None:
    run = execution_factory.begin_execution(scope=ExecutionScope.CHAT_TURN)
    with pytest.raises(asyncio.CancelledError):
        try:
            raise asyncio.CancelledError
        finally:
            execution_factory.end_execution(run.run_id)
    assert registry.active_count() == 0


def test_blocked_run_is_retained(registry: ExecutionRunRegistry) -> None:
    run = execution_factory.begin_execution(scope=ExecutionScope.CHAT_TURN)
    run.pause_token = PauseToken(scope=ExecutionScope.CHAT_TURN)

    removed = execution_factory.end_execution_unless_blocked(run.run_id)

    assert removed is False
    assert registry.active_count() == 1
    assert registry.get(run.run_id) is run


def test_unblocked_run_is_removed(registry: ExecutionRunRegistry) -> None:
    run = execution_factory.begin_execution(scope=ExecutionScope.CHAT_TURN)

    removed = execution_factory.end_execution_unless_blocked(run.run_id)

    assert removed is True
    assert registry.active_count() == 0


# ---------------------------------------------------------------------------
# Zombie reclamation (TTL lazy sweep)
# ---------------------------------------------------------------------------


def test_sweep_evicts_stale_runs() -> None:
    reg = ExecutionRunRegistry(ttl_seconds=100.0)
    # ``registered_at`` uses a monotonic clock; forge an ancient timestamp.
    stale = ExecutionRun(scope=ExecutionScope.CHAT_TURN, prompt_id="old", registered_at=-1000.0)
    fresh = ExecutionRun(scope=ExecutionScope.CHAT_TURN, prompt_id="new")
    reg.register(stale)
    reg.register(fresh)

    evicted = reg.sweep_stale()

    assert evicted == 1
    assert reg.get(stale.run_id) is None
    assert reg.get_by_prompt_id("old") is None
    assert reg.get(fresh.run_id) is fresh


def test_sweep_evicts_abandoned_blocked_run() -> None:
    """A pause that is never resumed must eventually be reclaimed."""
    reg = ExecutionRunRegistry(ttl_seconds=100.0)
    abandoned = ExecutionRun(scope=ExecutionScope.CHAT_TURN, registered_at=-1000.0)
    abandoned.pause_token = PauseToken(scope=ExecutionScope.CHAT_TURN)
    reg.register(abandoned)

    assert reg.sweep_stale() == 1
    assert reg.active_count() == 0


def test_ttl_disabled_never_evicts() -> None:
    reg = ExecutionRunRegistry(ttl_seconds=None)
    ancient = ExecutionRun(scope=ExecutionScope.CHAT_TURN, registered_at=-10_000.0)
    reg.register(ancient)

    assert reg.sweep_stale() == 0
    assert reg.active_count() == 1


def test_sweep_is_throttled_on_register() -> None:
    """Registering many runs triggers at most one sweep per interval."""
    calls: list[float | None] = []
    reg = ExecutionRunRegistry(ttl_seconds=100.0, sweep_interval_seconds=3600.0)
    original = reg.sweep_stale

    def _tracking(*, now: float | None = None) -> int:
        calls.append(now)
        return original(now=now)

    reg.sweep_stale = _tracking  # type: ignore[method-assign]
    for _ in range(5):
        reg.register(ExecutionRun(scope=ExecutionScope.CHAT_TURN))

    # The constructor primed ``_last_sweep``; within the interval no sweep fires.
    assert calls == []
    assert reg.active_count() == 5
