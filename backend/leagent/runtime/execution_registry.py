"""In-process registry for :class:`ExecutionRun` handles.

The registry is the in-process source of truth for active runs (observability,
cross-scope linking, and pause/resume lookup). Completed runs are retired
explicitly via :func:`leagent.runtime.execution_factory.end_execution`.

Paused runs are *intentionally* retained (see ``end_execution_unless_blocked``)
so a later resume can re-link them. That retention is the one unbounded-growth
vector: a turn that pauses ``awaiting_user_input`` and is never resumed (client
closes the tab, task abandoned) would otherwise live forever. To bound memory
in long-running single-process servers, the registry performs a throttled,
TTL-based lazy sweep that evicts runs older than ``ttl_seconds``. Durable resume
is unaffected because it flows through the ``CheckpointStore`` /
``WorkflowStateStore`` (keyed by ``checkpoint_id`` / ``workflow_state_id``), not
the in-memory handle.
"""

from __future__ import annotations

import os
from time import monotonic

from leagent.runtime.execution_run import ExecutionRun
from leagent.utils.logging import get_logger

logger = get_logger(__name__)

# Default retention for an in-memory run handle. A run still alive after this
# many seconds is almost certainly abandoned (its durable checkpoint, if any,
# outlives the handle). Generous by default so legitimate long pauses and
# same-day resumes are never dropped.
_DEFAULT_TTL_SECONDS = 6 * 60 * 60  # 6 hours
# Minimum spacing between sweeps so ``register`` stays O(1) amortised.
_DEFAULT_SWEEP_INTERVAL_SECONDS = 60.0


def _ttl_from_env() -> float | None:
    """Read the TTL override from the environment.

    ``LEAGENT_EXECUTION_RUN_TTL_SECONDS`` set to ``0`` (or a negative value)
    disables eviction entirely.
    """
    raw = os.getenv("LEAGENT_EXECUTION_RUN_TTL_SECONDS")
    if raw is None:
        return _DEFAULT_TTL_SECONDS
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return _DEFAULT_TTL_SECONDS
    return value if value > 0 else None


class ExecutionRunRegistry:
    """Track active runs for observability and cross-scope linking."""

    def __init__(
        self,
        *,
        ttl_seconds: float | None = _DEFAULT_TTL_SECONDS,
        sweep_interval_seconds: float = _DEFAULT_SWEEP_INTERVAL_SECONDS,
    ) -> None:
        self._runs: dict[str, ExecutionRun] = {}
        self._prompt_index: dict[str, str] = {}
        self._ttl_seconds = ttl_seconds
        self._sweep_interval_seconds = max(0.0, sweep_interval_seconds)
        self._last_sweep = monotonic()

    def register(self, run: ExecutionRun) -> ExecutionRun:
        self._runs[run.run_id] = run
        if run.prompt_id:
            self._prompt_index[run.prompt_id] = run.run_id
        # Reclaim abandoned handles opportunistically on the hot path; the sweep
        # is throttled so this stays amortised O(1).
        self._maybe_sweep()
        return run

    def get(self, run_id: str) -> ExecutionRun | None:
        return self._runs.get(run_id)

    def remove(self, run_id: str) -> None:
        run = self._runs.pop(run_id, None)
        # Only drop the prompt index entry if it still points at this run —
        # a newer run may have reused the same prompt_id.
        if run is not None and run.prompt_id and self._prompt_index.get(run.prompt_id) == run_id:
            self._prompt_index.pop(run.prompt_id, None)

    def list_for_session(self, session_id: str) -> list[ExecutionRun]:
        return [r for r in self._runs.values() if r.session_id == session_id]

    def get_by_prompt_id(self, prompt_id: str) -> ExecutionRun | None:
        run_id = self._prompt_index.get(prompt_id)
        if run_id is not None:
            return self._runs.get(run_id)
        return None

    def get_active_chat_turn(self, session_id: str) -> ExecutionRun | None:
        """Return the most recently registered chat-turn run for *session_id*.

        ``_runs`` is an insertion-ordered ``dict``, so registration order is the
        ground truth for "most recent" — more reliable than the coarse
        ``monotonic`` clock, which can tie on rapid successive calls (Windows
        resolution is ~15ms). Iterate in order and keep the last match.
        """
        active: ExecutionRun | None = None
        for r in self._runs.values():
            if r.session_id == session_id and r.scope.value == "chat_turn":
                active = r
        return active

    def active_count(self) -> int:
        """Number of runs currently retained (observability / tests)."""
        return len(self._runs)

    def _maybe_sweep(self) -> None:
        if self._ttl_seconds is None:
            return
        now = monotonic()
        if now - self._last_sweep < self._sweep_interval_seconds:
            return
        self._last_sweep = now
        self.sweep_stale(now=now)

    def sweep_stale(self, *, now: float | None = None) -> int:
        """Evict runs older than ``ttl_seconds``. Returns the count evicted.

        Public so a scheduler (or a test) can force a sweep. Eviction is a
        no-op when eviction is disabled (``ttl_seconds`` is ``None``).
        """
        if self._ttl_seconds is None:
            return 0
        current = monotonic() if now is None else now
        cutoff = self._ttl_seconds
        stale = [
            run_id
            for run_id, run in self._runs.items()
            if current - run.registered_at > cutoff
        ]
        for run_id in stale:
            self.remove(run_id)
        if stale:
            logger.info(
                "execution_run_registry_evicted_stale",
                evicted=len(stale),
                remaining=len(self._runs),
                ttl_seconds=self._ttl_seconds,
            )
        return len(stale)


_registry: ExecutionRunRegistry | None = None


def get_execution_run_registry() -> ExecutionRunRegistry:
    global _registry
    if _registry is None:
        _registry = ExecutionRunRegistry(ttl_seconds=_ttl_from_env())
    return _registry


__all__ = ["ExecutionRunRegistry", "get_execution_run_registry"]
