"""Pluggable backend for tracking active :class:`ExecutionRun` handles.

``ExecutionRunRegistry`` (in :mod:`StAgent.runtime.execution_registry`) is the
default, in-process implementation and the only one used by the standalone
single-worker deployment. This module defines the :class:`RunStore` protocol it
satisfies and a process-wide accessor (:func:`get_run_store`) so a multi-worker
deployment can swap in a shared backend (e.g. :class:`RedisRunStore`) at startup
via :func:`set_run_store` without touching any call site.

By default :func:`get_run_store` returns the in-process registry singleton, so
behaviour is identical to calling ``get_execution_run_registry()`` directly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from StAgent.runtime.execution_registry import get_execution_run_registry

if TYPE_CHECKING:
    from StAgent.runtime.execution_run import ExecutionRun


@runtime_checkable
class RunStore(Protocol):
    """Backend contract for the active-run registry.

    Every method is synchronous: the hot paths that mint/retire runs are called
    from both sync and async contexts, so an implementation backed by a network
    store must use a synchronous client (or an in-memory write-through cache).
    """

    def register(self, run: ExecutionRun) -> ExecutionRun: ...

    def get(self, run_id: str) -> ExecutionRun | None: ...

    def remove(self, run_id: str) -> None: ...

    def list_for_session(self, session_id: str) -> list[ExecutionRun]: ...

    def get_by_prompt_id(self, prompt_id: str) -> ExecutionRun | None: ...

    def get_active_chat_turn(self, session_id: str) -> ExecutionRun | None: ...


_run_store: RunStore | None = None


def get_run_store() -> RunStore:
    """Return the active run store (defaults to the in-process registry)."""
    if _run_store is not None:
        return _run_store
    return get_execution_run_registry()


def set_run_store(store: RunStore | None) -> None:
    """Install a process-wide run store (e.g. at multi-worker startup).

    Passing ``None`` restores the default in-process registry. The store must
    satisfy :class:`RunStore`.
    """
    global _run_store
    if store is not None and not isinstance(store, RunStore):
        raise TypeError("store must implement the RunStore protocol")
    _run_store = store


def reset_run_store() -> None:
    """Restore the default in-process run store (primarily for tests)."""
    global _run_store
    _run_store = None


__all__ = ["RunStore", "get_run_store", "set_run_store", "reset_run_store"]
