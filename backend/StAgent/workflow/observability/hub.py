"""Process-level aggregation of workflow-execution observability.

One hub per process. It is a **bypass consumer** of the progress-event stream,
wired next to ``_publish_to_event_bus``: one event, two independent consumers.
Every public method is exception-safe by construction, and the caller wraps
``on_event`` in a ``try`` — an observability failure must never disturb an
execution.

State is bounded: at most ``max_tracked`` charts are retained, evicted oldest
first, with live executions protected from eviction. There is no time-series
store — a finished chart that has fallen out of the cache is rebuilt on demand
from ``execution_history``.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

import structlog

from StAgent.workflow.engine.progress import now_ms

from .gantt import BAR_BLOCKED, BAR_RUNNING, ExecutionGantt, GanttBuilder
from .profile import ExecutionProfile, build_profile

logger = structlog.get_logger(__name__)

__all__ = ["LiveExecution", "ObservabilityHub", "get_observability_hub"]

#: Chart statuses that mean "this execution is not done yet".
_ACTIVE_STATUSES = frozenset({BAR_RUNNING, "pending", "queued"})


@dataclass
class LiveExecution:
    """A single currently-executing workflow, as seen by the monitoring wall."""

    prompt_id: str
    execution_id: str | None = None
    flow_id: str | None = None
    flow_name: str | None = None
    status: str = BAR_RUNNING
    started_at_ms: int = 0
    #: Nodes with an open bar right now (parallel branches → several entries).
    current_nodes: list[str] = field(default_factory=list)
    node_total: int = 0
    node_done: int = 0
    #: ``None`` unless a broker queue is configured — the in-process default has
    #: no queue to be positioned in.
    queue_position: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "prompt_id": self.prompt_id,
            "execution_id": self.execution_id,
            "flow_id": self.flow_id,
            "flow_name": self.flow_name,
            "status": self.status,
            "started_at_ms": self.started_at_ms,
            "current_nodes": list(self.current_nodes),
            "node_total": self.node_total,
            "node_done": self.node_done,
            "queue_position": self.queue_position,
        }


class ObservabilityHub:
    """Live-execution registry plus a bounded cache of Gantt charts."""

    def __init__(self, *, max_tracked: int = 256) -> None:
        self._max_tracked = max(1, max_tracked)
        #: prompt_id → builder, in insertion order (oldest first) for eviction.
        self._builders: OrderedDict[str, GanttBuilder] = OrderedDict()
        self._lock = threading.RLock()

    # -- ingest -------------------------------------------------------------

    def on_event(self, event: Any) -> None:
        """Fold one progress event into the registry.

        Never raises: a malformed event is logged at debug level and dropped.
        """
        try:
            prompt_id = getattr(event, "prompt_id", None) or (
                event.get("prompt_id") if isinstance(event, dict) else None
            )
            if not prompt_id:
                return
            with self._lock:
                builder = self._builders.get(prompt_id)
                if builder is None:
                    builder = GanttBuilder(str(prompt_id))
                    self._builders[str(prompt_id)] = builder
                    self._evict_locked()
                builder.apply(event)
        except Exception:  # noqa: BLE001 - bypass consumer, must not propagate
            logger.debug("observability_hub_event_failed", exc_info=True)

    # -- read ---------------------------------------------------------------

    def live_executions(self) -> list[LiveExecution]:
        """Executions with no terminal event yet, newest first."""
        out: list[LiveExecution] = []
        with self._lock:
            builders = list(self._builders.values())
        for builder in builders:
            if builder.finished:
                continue
            try:
                out.append(self._to_live(builder.snapshot()))
            except Exception:  # noqa: BLE001
                logger.debug("observability_hub_live_failed", exc_info=True)
        out.sort(key=lambda e: e.started_at_ms, reverse=True)
        return out

    def gantt(self, prompt_id: str) -> ExecutionGantt | None:
        """The in-memory chart for *prompt_id*, or ``None`` if not tracked."""
        with self._lock:
            builder = self._builders.get(prompt_id)
        if builder is None:
            return None
        try:
            return builder.snapshot()
        except Exception:  # noqa: BLE001
            logger.debug("observability_hub_gantt_failed", exc_info=True)
            return None

    def profile(self, prompt_id: str) -> ExecutionProfile | None:
        """Bottleneck analysis for a tracked execution."""
        chart = self.gantt(prompt_id)
        if chart is None:
            return None
        try:
            return build_profile(chart)
        except Exception:  # noqa: BLE001
            logger.debug("observability_hub_profile_failed", exc_info=True)
            return None

    def tracked_prompt_ids(self) -> list[str]:
        with self._lock:
            return list(self._builders.keys())

    # -- housekeeping -------------------------------------------------------

    def gc(self, *, keep_finished: int | None = None) -> int:
        """Drop finished charts beyond *keep_finished*; returns the number evicted.

        Live executions are never collected. Defaults to keeping half the budget
        for finished charts so a burst of new runs cannot starve the wall.
        """
        budget = self._max_tracked // 2 if keep_finished is None else max(0, keep_finished)
        removed = 0
        with self._lock:
            finished = [pid for pid, b in self._builders.items() if b.finished]
            for prompt_id in finished[: max(0, len(finished) - budget)]:
                self._builders.pop(prompt_id, None)
                removed += 1
        return removed

    def forget(self, prompt_id: str) -> None:
        with self._lock:
            self._builders.pop(prompt_id, None)

    def _evict_locked(self) -> None:
        """Trim to ``max_tracked``, preferring finished charts as victims."""
        while len(self._builders) > self._max_tracked:
            victim = next(
                (pid for pid, b in self._builders.items() if b.finished),
                next(iter(self._builders)),
            )
            self._builders.pop(victim, None)

    # -- projection ---------------------------------------------------------

    @staticmethod
    def _to_live(chart: ExecutionGantt) -> LiveExecution:
        open_bars = [b for b in chart.bars if b.open]
        blocked = [b for b in open_bars if b.status == BAR_BLOCKED]
        status = chart.status
        if blocked:
            status = "waiting_human"
        elif status in _ACTIVE_STATUSES and not open_bars:
            status = "queued"
        done = len({b.node_id for b in chart.bars if not b.open})
        return LiveExecution(
            prompt_id=chart.prompt_id,
            execution_id=chart.execution_id,
            flow_id=chart.flow_id,
            flow_name=chart.flow_name,
            status=status,
            started_at_ms=chart.started_at_ms or now_ms(),
            current_nodes=[b.node_id for b in open_bars],
            node_total=max(chart.node_total, done + len(open_bars)),
            node_done=done,
        )


_hub: ObservabilityHub | None = None
_hub_lock = threading.Lock()


def get_observability_hub() -> ObservabilityHub:
    """Return the process-wide hub, creating it on first use."""
    global _hub
    if _hub is None:
        with _hub_lock:
            if _hub is None:
                _hub = ObservabilityHub()
    return _hub
