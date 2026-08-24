"""Execution profiling: where did an execution's wall-clock time actually go?

Derived entirely from an :class:`~StAgent.workflow.observability.gantt.ExecutionGantt`
— no extra instrumentation, no time-series store. Every number here is a
descriptive statistic over bar intervals, so it works identically for a live
chart, a finished one, and an approximate replay.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from StAgent.workflow.engine.progress import now_ms

from .gantt import BAR_BLOCKED, BAR_CACHED, ExecutionGantt, GanttBar

__all__ = ["ExecutionProfile", "NodeShare", "build_profile"]

KIND_COMPUTE = "compute"
KIND_WAITING_HUMAN = "waiting_human"
KIND_CACHED_SAVING = "cached_saving"
KIND_RETRY = "retry"


@dataclass
class NodeShare:
    """One node's slice of the execution's time composition."""

    node_id: str
    class_type: str
    duration_ms: int
    #: Fraction of *actual* elapsed node time (compute + human wait). A
    #: ``cached_saving`` share is counter-factual, so its ratio is against that
    #: same denominator and may exceed the 0..1 range in aggregate.
    share: float
    kind: str = KIND_COMPUTE
    display_name: str = ""
    iteration: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "class_type": self.class_type,
            "display_name": self.display_name or self.node_id,
            "duration_ms": self.duration_ms,
            "share": round(self.share, 6),
            "kind": self.kind,
            "iteration": self.iteration,
        }


@dataclass
class ExecutionProfile:
    """Bottleneck analysis for one execution."""

    prompt_id: str
    #: Wall-clock width of the whole chart.
    total_ms: int = 0
    #: Wall time during which *at least one* node occupied the timeline
    #: (sweep-line union of all bar intervals). Equals the true critical path
    #: for a serial chain and is a lower bound for a parallel one.
    critical_path_ms: int = 0
    bottleneck: NodeShare | None = None
    waiting_human_ms: int = 0
    #: Estimated: a cache hit costs ~0, so the saving is approximated from what
    #: the same node (or the same node type) cost when it did run.
    cached_saved_ms: int = 0
    #: Σ node durations ÷ critical path. ``1.0`` means strictly serial.
    parallelism: float = 0.0
    #: Busy wall time ÷ total wall time. Below 1.0 means scheduling or waiting gaps.
    timeline_efficiency: float = 0.0
    shares: list[NodeShare] = field(default_factory=list)
    bar_count: int = 0
    approximate: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "prompt_id": self.prompt_id,
            "total_ms": self.total_ms,
            "critical_path_ms": self.critical_path_ms,
            "bottleneck": self.bottleneck.to_dict() if self.bottleneck else None,
            "waiting_human_ms": self.waiting_human_ms,
            "cached_saved_ms": self.cached_saved_ms,
            "parallelism": round(self.parallelism, 4),
            "timeline_efficiency": round(self.timeline_efficiency, 4),
            "shares": [s.to_dict() for s in self.shares],
            "bar_count": self.bar_count,
            "approximate": self.approximate,
        }


def build_profile(gantt: ExecutionGantt, *, now: int | None = None) -> ExecutionProfile:
    """Summarize *gantt* into an :class:`ExecutionProfile`.

    Open bars (a running node, or one hanging on a human) are measured up to
    *now* — defaulting to the server clock — so a live profile keeps updating
    instead of reporting zero.
    """
    reference = now if now is not None else now_ms()
    end = gantt.finished_at_ms if gantt.finished_at_ms is not None else reference
    profile = ExecutionProfile(
        prompt_id=gantt.prompt_id,
        total_ms=max(0, end - gantt.started_at_ms),
        bar_count=len(gantt.bars),
        approximate=gantt.approximate,
    )
    if not gantt.bars:
        return profile

    spans = [(b.started_at_ms, b.end_or(end)) for b in gantt.bars]
    profile.critical_path_ms = _union_length(spans)

    ordered = sorted(gantt.bars, key=lambda b: (b.started_at_ms, b.node_id))
    compute: list[NodeShare] = []
    waiting: list[NodeShare] = []
    cached: list[NodeShare] = []

    for index, bar in enumerate(ordered):
        span = max(0, bar.end_or(end) - bar.started_at_ms)
        if bar.status == BAR_CACHED or bar.cached:
            saving = _estimate_cache_saving(bar, gantt.bars)
            profile.cached_saved_ms += saving
            if saving > 0:
                cached.append(_share(bar, saving, KIND_CACHED_SAVING))
            continue
        if bar.status == BAR_BLOCKED:
            wait = _human_wait_ms(bar, span, ordered, index, end)
            compute_ms = max(0, span - wait)
            profile.waiting_human_ms += wait
            if compute_ms > 0:
                compute.append(_share(bar, compute_ms, KIND_COMPUTE))
            if wait > 0:
                waiting.append(_share(bar, wait, KIND_WAITING_HUMAN))
            continue
        kind = KIND_RETRY if bar.detail.get("retry") else KIND_COMPUTE
        compute.append(_share(bar, span, kind))

    spent = sum(s.duration_ms for s in compute) + sum(s.duration_ms for s in waiting)
    denominator = float(spent) if spent > 0 else 1.0
    for share in (*compute, *waiting, *cached):
        share.share = share.duration_ms / denominator
    profile.shares = sorted(
        (*compute, *waiting, *cached), key=lambda s: s.duration_ms, reverse=True
    )

    if compute:
        profile.bottleneck = max(compute, key=lambda s: s.duration_ms)
    if profile.critical_path_ms > 0:
        profile.parallelism = spent / profile.critical_path_ms
    if profile.total_ms > 0:
        profile.timeline_efficiency = profile.critical_path_ms / profile.total_ms
    return profile


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------


def _share(bar: GanttBar, duration_ms: int, kind: str) -> NodeShare:
    return NodeShare(
        node_id=bar.node_id,
        class_type=bar.class_type,
        display_name=bar.display_name or bar.node_id,
        duration_ms=duration_ms,
        share=0.0,
        kind=kind,
        iteration=bar.iteration,
    )


def _union_length(spans: list[tuple[int, int]]) -> int:
    """Total length covered by *spans*, counting overlaps once (sweep line)."""
    if not spans:
        return 0
    ordered = sorted((min(a, b), max(a, b)) for a, b in spans)
    total = 0
    cursor_start, cursor_end = ordered[0]
    for start, stop in ordered[1:]:
        if start > cursor_end:
            total += cursor_end - cursor_start
            cursor_start, cursor_end = start, stop
        else:
            cursor_end = max(cursor_end, stop)
    total += cursor_end - cursor_start
    return max(0, total)


def _human_wait_ms(
    bar: GanttBar,
    span: int,
    ordered: list[GanttBar],
    index: int,
    end: int,
) -> int:
    """How long *bar* sat waiting on a person.

    A live blocked bar hangs open, so the wait is whatever exceeds the node's own
    compute time. A replayed one was clamped shut at its compute end, so the wait
    instead shows up as the gap before the next bar starts (or before the chart
    ends, if the run is still parked on the review).
    """
    compute_ms = bar.detail.get("compute_ms")
    known_compute = int(compute_ms) if isinstance(compute_ms, (int, float)) else 0
    inline_wait = max(0, span - known_compute)
    if inline_wait > 0:
        return inline_wait
    bar_end = bar.end_or(end)
    successor = next(
        (b.started_at_ms for b in ordered[index + 1 :] if b.started_at_ms >= bar_end),
        None,
    )
    return max(0, (successor if successor is not None else end) - bar_end)


def _estimate_cache_saving(bar: GanttBar, bars: list[GanttBar]) -> int:
    """Approximate what a cache hit avoided paying.

    Best evidence first: the same node's own non-cached runs in this execution,
    then any node of the same type. With neither, we decline to guess and return
    ``0`` rather than inventing a number.
    """
    for predicate in (
        lambda b: b.node_id == bar.node_id,
        lambda b: bool(bar.class_type) and b.class_type == bar.class_type,
    ):
        samples = [
            b.duration_ms
            for b in bars
            if b is not bar
            and not b.cached
            and b.status not in (BAR_CACHED, BAR_BLOCKED)
            and b.duration_ms > 0
            and predicate(b)
        ]
        if samples:
            return int(sum(samples) / len(samples))
    return 0
