"""Gantt-chart construction for a single workflow execution.

Two entry points, one output shape:

* :class:`GanttBuilder` accumulates an :class:`ExecutionGantt` incrementally from
  the live :class:`~StAgent.workflow.engine.progress.ProgressEvent` stream.
* :meth:`GanttBuilder.from_execution_history` rebuilds one from a persisted
  ``workflow_executions`` row so a finished run can be replayed.

The replay path degrades gracefully: rows written before node timestamps existed
carry no ``started_at_ms``, so the bars are stitched end-to-end in
``duration_ms`` order and the chart is flagged ``approximate`` for the UI to
label. Nothing here touches execution — see the package docstring.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any

from StAgent.workflow.engine.progress import now_ms

__all__ = ["ExecutionGantt", "GanttBar", "GanttBuilder"]

#: Bar statuses, mirroring ``NodeStatus`` plus the chart-level terminal states.
BAR_RUNNING = "running"
BAR_SUCCESS = "success"
BAR_ERROR = "error"
BAR_BLOCKED = "blocked"
BAR_CACHED = "cached"
BAR_SKIPPED = "skipped"

#: ``execution_*`` event suffixes that do **not** end the chart.
_NON_TERMINAL_EXECUTION_EVENTS = frozenset({"execution_start", "execution_blocked"})

#: Persisted ``WorkflowStatus`` value → bar status.
_HISTORY_STATUS_MAP: dict[str, str] = {
    "completed": BAR_SUCCESS,
    "success": BAR_SUCCESS,
    "failed": BAR_ERROR,
    "error": BAR_ERROR,
    "timeout": BAR_ERROR,
    "cancelled": BAR_ERROR,
    "waiting_human": BAR_BLOCKED,
    "paused": BAR_BLOCKED,
    "running": BAR_RUNNING,
    "pending": BAR_RUNNING,
}


@dataclass
class GanttBar:
    """One node's occupancy of one contiguous slice of wall-clock time.

    A node owns several bars when it runs more than once — a loop re-entry or a
    resume after a human review each open a fresh bar with the next
    ``iteration``.
    """

    node_id: str
    class_type: str = ""
    display_name: str = ""
    started_at_ms: int = 0
    #: ``None`` while the node is still running (or hanging on a human).
    finished_at_ms: int | None = None
    status: str = BAR_RUNNING
    lane: int = 0
    cached: bool = False
    iteration: int = 0
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def duration_ms(self) -> int:
        """Closed width in ms; ``0`` while the bar is still open."""
        if self.finished_at_ms is None:
            return 0
        return max(0, self.finished_at_ms - self.started_at_ms)

    @property
    def open(self) -> bool:
        """Whether the bar has no end yet."""
        return self.finished_at_ms is None

    def end_or(self, fallback: int) -> int:
        """End timestamp, substituting *fallback* for an open bar."""
        return self.finished_at_ms if self.finished_at_ms is not None else fallback

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "class_type": self.class_type,
            "display_name": self.display_name or self.node_id,
            "started_at_ms": self.started_at_ms,
            "finished_at_ms": self.finished_at_ms,
            "duration_ms": self.duration_ms,
            "status": self.status,
            "lane": self.lane,
            "cached": self.cached,
            "iteration": self.iteration,
            "detail": dict(self.detail),
        }


@dataclass
class ExecutionGantt:
    """A full execution rendered as lanes of :class:`GanttBar`."""

    prompt_id: str
    execution_id: str | None = None
    flow_id: str | None = None
    flow_name: str | None = None
    started_at_ms: int = 0
    finished_at_ms: int | None = None
    status: str = BAR_RUNNING
    bars: list[GanttBar] = field(default_factory=list)
    lane_count: int = 1
    #: Timestamps were reconstructed rather than recorded — label the axis
    #: "approximate timeline" in the UI.
    approximate: bool = False
    #: Node count declared by the flow (bars may exceed it thanks to loops).
    node_total: int = 0

    @property
    def duration_ms(self) -> int:
        """Chart width in ms, measured to *now* while the execution runs."""
        end = self.finished_at_ms if self.finished_at_ms is not None else now_ms()
        return max(0, end - self.started_at_ms)

    def assign_lanes(self) -> None:
        """Greedy interval colouring: pack bars into the fewest non-overlapping rows.

        Bars are visited in start order and dropped into the first lane whose
        previous bar has already ended, so a serial chain collapses onto one row
        while parallel branches stagger down into their own rows. An open bar
        occupies its lane indefinitely — it has not ended yet, so nothing may be
        packed behind it.
        """
        self.bars.sort(key=lambda b: (b.started_at_ms, b.iteration, b.node_id))
        lane_ends: list[float] = []
        for bar in self.bars:
            end = float(bar.finished_at_ms) if bar.finished_at_ms is not None else float("inf")
            for lane, lane_end in enumerate(lane_ends):
                if lane_end <= bar.started_at_ms:
                    bar.lane = lane
                    lane_ends[lane] = end
                    break
            else:
                bar.lane = len(lane_ends)
                lane_ends.append(end)
        self.lane_count = max(1, len(lane_ends))

    def to_dict(self) -> dict[str, Any]:
        return {
            "prompt_id": self.prompt_id,
            "execution_id": self.execution_id,
            "flow_id": self.flow_id,
            "flow_name": self.flow_name,
            "started_at_ms": self.started_at_ms,
            "finished_at_ms": self.finished_at_ms,
            "status": self.status,
            "lane_count": self.lane_count,
            "approximate": self.approximate,
            "node_total": self.node_total,
            "bars": [b.to_dict() for b in self.bars],
            # Server clock, so a client can grow open bars without trusting its own.
            "server_now_ms": now_ms(),
        }


class GanttBuilder:
    """Accumulates an :class:`ExecutionGantt` from a progress-event stream.

    ``apply`` tolerates duplicate events (a re-delivered ``node_success`` finds
    no open bar and is a no-op; a retry's repeated ``executing`` re-labels the
    open bar instead of opening a second one) and events for unknown nodes.
    """

    def __init__(self, prompt_id: str, *, flow_meta: dict[str, Any] | None = None) -> None:
        meta = flow_meta or {}
        self._gantt = ExecutionGantt(
            prompt_id=prompt_id,
            execution_id=_opt_str(meta.get("execution_id")),
            flow_id=_opt_str(meta.get("flow_id")),
            flow_name=_opt_str(meta.get("flow_name")),
            started_at_ms=int(meta.get("started_at_ms") or 0),
            node_total=int(meta.get("node_total") or 0),
        )
        self._open: dict[str, GanttBar] = {}
        self._next_iteration: dict[str, int] = {}
        self._catalogue: dict[str, dict[str, str]] = dict(meta.get("nodes") or {})

    # -- public API ---------------------------------------------------------

    @property
    def prompt_id(self) -> str:
        return self._gantt.prompt_id

    @property
    def status(self) -> str:
        return self._gantt.status

    @property
    def finished(self) -> bool:
        """Whether a terminal ``execution_*`` event has closed the chart."""
        return self._gantt.finished_at_ms is not None

    def snapshot(self) -> ExecutionGantt:
        """A lane-assigned deep copy, safe to serialize while events keep arriving."""
        clone = replace(
            self._gantt,
            bars=[copy.deepcopy(b) for b in self._gantt.bars],
        )
        clone.assign_lanes()
        return clone

    def apply(self, event: Any) -> None:
        """Fold one progress event into the chart."""
        etype = _field(event, "type") or ""
        if not isinstance(etype, str) or not etype:
            return
        node_id = _opt_str(_field(event, "node_id"))
        state = _field(event, "state")
        data = _field(event, "data") or {}
        if not isinstance(data, dict):
            data = {}

        if etype == "execution_start":
            self._on_execution_start(data)
            return
        if etype.startswith("execution_") and etype not in _NON_TERMINAL_EXECUTION_EVENTS:
            self._on_execution_end(etype, data)
            return
        if node_id is None:
            return
        if etype == "executing":
            self._on_executing(node_id, state)
        elif etype == "node_cached":
            self._on_instant(node_id, state, BAR_CACHED, cached=True)
        elif etype == "node_skipped":
            self._on_instant(node_id, state, BAR_SKIPPED)
        elif etype == "node_success":
            self._close(node_id, state, BAR_SUCCESS)
        elif etype == "node_error":
            self._close(node_id, state, BAR_ERROR, error=_state_field(state, "error"))
        elif etype == "node_blocked":
            self._on_blocked(node_id, state)
        elif etype == "executed":
            # Fallback: the engine emits ``executed`` after ``node_success``;
            # only acts when something upstream dropped the close event.
            self._close(node_id, state, BAR_SUCCESS)
        elif etype == "progress":
            self._on_progress(node_id, state)

    # -- event handlers -----------------------------------------------------

    def _on_execution_start(self, data: dict[str, Any]) -> None:
        g = self._gantt
        g.started_at_ms = int(data.get("started_at_ms") or g.started_at_ms or now_ms())
        g.status = BAR_RUNNING
        g.finished_at_ms = None
        if name := _opt_str(data.get("flow_name")):
            g.flow_name = name
        if flow_id := _opt_str(data.get("workflow_id")):
            g.flow_id = g.flow_id or flow_id
        if total := data.get("node_total"):
            g.node_total = int(total)
        catalogue = data.get("nodes")
        if isinstance(catalogue, dict):
            for node_id, entry in catalogue.items():
                if isinstance(entry, dict):
                    self._catalogue[str(node_id)] = {
                        "class_type": str(entry.get("class_type") or ""),
                        "display_name": str(entry.get("display_name") or node_id),
                    }

    def _on_execution_end(self, etype: str, data: dict[str, Any]) -> None:
        g = self._gantt
        end = int(data.get("finished_at_ms") or now_ms())
        g.finished_at_ms = end
        g.status = etype[len("execution_") :] or BAR_SUCCESS
        # Clamp anything still open so a persisted chart has no infinite bars.
        for bar in g.bars:
            if bar.open:
                bar.finished_at_ms = max(bar.started_at_ms, end)
        self._open.clear()

    def _on_executing(self, node_id: str, state: Any) -> None:
        started = _state_field(state, "started_at_ms")
        existing = self._open.get(node_id)
        if existing is not None:
            if started is None or int(started) == existing.started_at_ms:
                # Same span re-announced: a retry attempt. Label, do not split.
                self._note_retry(existing, state)
                return
            # A genuinely new span while the old bar is open (dropped close event).
            existing.finished_at_ms = max(existing.started_at_ms, int(started))
        bar = self._new_bar(node_id, int(started) if started is not None else now_ms())
        bar.status = BAR_RUNNING
        self._note_retry(bar, state)
        self._open[node_id] = bar
        self._gantt.bars.append(bar)

    def _on_instant(
        self, node_id: str, state: Any, status: str, *, cached: bool = False
    ) -> None:
        """A node that never ran: a cache hit or a muted/bypassed node.

        Still gets a (zero-width) bar — a skipped step occupying its slot on the
        timeline is the point, not an omission.
        """
        stamp = _state_field(state, "started_at_ms")
        end = _state_field(state, "finished_at_ms")
        at = int(stamp if stamp is not None else end if end is not None else now_ms())
        existing = self._open.pop(node_id, None)
        if existing is not None:
            existing.status = status
            existing.cached = cached
            existing.finished_at_ms = max(existing.started_at_ms, int(end or at))
            return
        bar = self._new_bar(node_id, at)
        bar.status = status
        bar.cached = cached
        bar.finished_at_ms = max(at, int(end)) if end is not None else at
        self._gantt.bars.append(bar)

    def _on_blocked(self, node_id: str, state: Any) -> None:
        """Hold the bar open so the human-wait stretch stays visible."""
        bar = self._open.get(node_id)
        if bar is None:
            stamp = _state_field(state, "started_at_ms")
            bar = self._new_bar(node_id, int(stamp) if stamp is not None else now_ms())
            self._open[node_id] = bar
            self._gantt.bars.append(bar)
        bar.status = BAR_BLOCKED
        bar.finished_at_ms = None
        compute_ms = _metadata_int(state, "duration_ms")
        if compute_ms is not None:
            bar.detail["compute_ms"] = compute_ms

    def _close(
        self, node_id: str, state: Any, status: str, *, error: Any = None
    ) -> None:
        bar = self._open.pop(node_id, None)
        if bar is None:
            return
        end = _state_field(state, "finished_at_ms")
        bar.finished_at_ms = max(
            bar.started_at_ms, int(end) if end is not None else now_ms()
        )
        # A bar that hung on a human keeps that identity; the wait is the story.
        if bar.status == BAR_BLOCKED and status == BAR_SUCCESS:
            bar.detail["resumed"] = True
        bar.status = status
        if isinstance(error, str) and error:
            bar.detail["error"] = error
        if (duration := _metadata_int(state, "duration_ms")) is not None:
            bar.detail["compute_ms"] = duration

    def _on_progress(self, node_id: str, state: Any) -> None:
        bar = self._open.get(node_id)
        if bar is None:
            return
        value = _state_field(state, "value")
        maximum = _state_field(state, "max")
        if value is not None:
            bar.detail["value"] = value
        if maximum is not None:
            bar.detail["max"] = maximum

    # -- helpers ------------------------------------------------------------

    def _new_bar(self, node_id: str, started_at_ms: int) -> GanttBar:
        entry = self._catalogue.get(node_id) or {}
        iteration = self._next_iteration.get(node_id, 0)
        self._next_iteration[node_id] = iteration + 1
        if not self._gantt.started_at_ms:
            self._gantt.started_at_ms = started_at_ms
        return GanttBar(
            node_id=node_id,
            class_type=str(entry.get("class_type") or ""),
            display_name=str(entry.get("display_name") or node_id),
            started_at_ms=started_at_ms,
            iteration=iteration,
        )

    @staticmethod
    def _note_retry(bar: GanttBar, state: Any) -> None:
        """Record the retry attempt as an assignment, so replays stay idempotent."""
        retry = _metadata_int(state, "retry")
        if retry is not None:
            bar.detail["retry"] = retry
        max_retries = _metadata_int(state, "max_retries")
        if max_retries is not None:
            bar.detail["max_retries"] = max_retries

    # -- replay -------------------------------------------------------------

    @classmethod
    def from_execution_history(cls, record: dict[str, Any]) -> ExecutionGantt:
        """Rebuild a chart from a persisted ``workflow_executions`` row.

        *record* is the shape returned by ``WorkflowService._record_to_dict``.
        Entries carrying ``started_at_ms`` are placed exactly; entries without
        one (written before the observability plane existed) are stitched
        end-to-end from their ``duration_ms`` and the chart is flagged
        ``approximate``.
        """
        history = record.get("execution_history")
        history = history if isinstance(history, list) else []
        origin = (
            _iso_to_ms(record.get("started_at"))
            or _iso_to_ms(record.get("created_at"))
            or _earliest_recorded_ms(history)
            or 0
        )
        gantt = ExecutionGantt(
            prompt_id=str(record.get("prompt_id") or ""),
            execution_id=_opt_str(record.get("id")),
            flow_id=_opt_str(record.get("flow_id")),
            flow_name=_opt_str(record.get("flow_name")),
            started_at_ms=origin,
            status=str(record.get("status") or BAR_SUCCESS),
            node_total=int(record.get("node_count") or 0),
        )

        iterations: dict[str, int] = {}
        cursor = origin
        for entry in history:
            if not isinstance(entry, dict):
                continue
            node_id = _opt_str(entry.get("node_id"))
            if node_id is None:
                continue
            duration = max(0, int(entry.get("duration_ms") or 0))
            started = entry.get("started_at_ms")
            finished = entry.get("finished_at_ms")
            if started is None:
                gantt.approximate = True
                started_ms = cursor
                finished_ms = cursor + duration
            else:
                started_ms = int(started)
                finished_ms = (
                    int(finished) if finished is not None else started_ms + duration
                )
            cursor = max(cursor, finished_ms)

            metadata = entry.get("metadata")
            metadata = metadata if isinstance(metadata, dict) else {}
            iteration = iterations.get(node_id, 0)
            iterations[node_id] = iteration + 1
            bar = GanttBar(
                node_id=node_id,
                class_type=str(metadata.get("class_type") or ""),
                display_name=node_id,
                started_at_ms=started_ms,
                finished_at_ms=max(started_ms, finished_ms),
                status=_replay_bar_status(entry, metadata),
                cached=bool(metadata.get("cached")),
                iteration=iteration,
                detail=_replay_detail(entry, metadata),
            )
            gantt.bars.append(bar)

        completed = _iso_to_ms(record.get("completed_at"))
        if completed is not None:
            gantt.finished_at_ms = max(completed, cursor)
        elif str(record.get("status") or "") in ("completed", "failed", "cancelled", "timeout"):
            duration_ms = int(record.get("duration_ms") or 0)
            gantt.finished_at_ms = max(origin + duration_ms, cursor)
        if not gantt.started_at_ms and gantt.bars:
            gantt.started_at_ms = min(b.started_at_ms for b in gantt.bars)
        gantt.assign_lanes()
        return gantt


# ---------------------------------------------------------------------------
# Field access helpers — events arrive as dataclasses (in-process) or as plain
# dicts (replayed from a WebSocket payload), so every read is duck-typed.
# ---------------------------------------------------------------------------


def _field(obj: Any, name: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _state_field(state: Any, name: str) -> Any:
    return _field(state, name)


def _metadata_int(state: Any, key: str) -> int | None:
    metadata = _field(state, "metadata")
    if not isinstance(metadata, dict):
        return None
    value = metadata.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _opt_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text or None


def _iso_to_ms(value: Any) -> int | None:
    """Parse a persisted ISO timestamp to epoch ms, treating naive input as UTC."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _earliest_recorded_ms(history: list[Any]) -> int | None:
    stamps = [
        int(e["started_at_ms"])
        for e in history
        if isinstance(e, dict) and e.get("started_at_ms") is not None
    ]
    return min(stamps) if stamps else None


def _replay_bar_status(entry: dict[str, Any], metadata: dict[str, Any]) -> str:
    if metadata.get("cached"):
        return BAR_CACHED
    if metadata.get("skipped_mode"):
        return BAR_SKIPPED
    raw = str(entry.get("status") or "").lower()
    return _HISTORY_STATUS_MAP.get(raw, BAR_SUCCESS)


def _replay_detail(entry: dict[str, Any], metadata: dict[str, Any]) -> dict[str, Any]:
    detail: dict[str, Any] = {}
    if error := entry.get("error"):
        detail["error"] = str(error)
    if (attempts := metadata.get("attempts")) is not None:
        detail["retry"] = attempts
    if (mode := metadata.get("skipped_mode")) is not None:
        detail["mode"] = mode
    if (duration := entry.get("duration_ms")) is not None:
        detail["compute_ms"] = int(duration or 0)
    return detail
