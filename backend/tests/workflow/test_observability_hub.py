"""ObservabilityHub tests, plus the engine → hub → chart integration path.

The hub is a **bypass consumer**: it must accumulate a faithful chart from the
real event stream, and it must never be able to disturb the execution that feeds
it. Both halves are covered here, the second by running actual workflows through
:class:`WorkflowExecutor` with the hub wired in as a progress handler.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from StAgent.workflow.base import WorkflowStatus
from StAgent.workflow.engine import WorkflowExecutor, build_cache_set
from StAgent.workflow.engine.progress import ProgressEvent
from StAgent.workflow.io import IO, NodeOutput, Schema
from StAgent.workflow.nodes import bootstrap, get_registry
from StAgent.workflow.nodes.base import WorkflowNode
from StAgent.workflow.observability import (
    GanttBuilder,
    ObservabilityHub,
    build_profile,
    get_observability_hub,
)
from StAgent.workflow.observability.gantt import (
    BAR_BLOCKED,
    BAR_RUNNING,
    BAR_SKIPPED,
)

ORIGIN = 1_700_000_000_000


# ---------------------------------------------------------------------------
# Hub unit tests
# ---------------------------------------------------------------------------


def _event(etype: str, *, prompt_id: str = "p1", node_id: str | None = None, **fields):
    """Build a progress event.

    Timestamps belong to ``state`` on node events and to ``data`` on
    ``execution_*`` events, mirroring what the engine actually emits.
    """
    state = None
    if not etype.startswith("execution_"):
        started = fields.pop("started_at_ms", None)
        finished = fields.pop("finished_at_ms", None)
        if started is not None or finished is not None:
            from StAgent.workflow.engine.progress import NodeProgressState

            state = NodeProgressState(
                node_id=node_id or "",
                started_at_ms=started,
                finished_at_ms=finished,
            )
    return ProgressEvent(
        type=etype, prompt_id=prompt_id, node_id=node_id, state=state, data=fields
    )


def test_hub_accumulates_a_chart_per_prompt() -> None:
    hub = ObservabilityHub(max_tracked=8)
    hub.on_event(_event("execution_start", started_at_ms=ORIGIN, node_total=2))
    hub.on_event(_event("executing", node_id="a", started_at_ms=ORIGIN + 10))
    hub.on_event(_event("executing", prompt_id="p2", node_id="z", started_at_ms=ORIGIN))

    assert sorted(hub.tracked_prompt_ids()) == ["p1", "p2"]
    chart = hub.gantt("p1")
    assert chart is not None
    assert [b.node_id for b in chart.bars] == ["a"]
    assert hub.gantt("nope") is None


def test_hub_projects_the_live_wall() -> None:
    hub = ObservabilityHub()
    hub.on_event(
        _event(
            "execution_start",
            started_at_ms=ORIGIN,
            node_total=3,
            flow_name="Demo",
            workflow_id="flow-1",
        )
    )
    hub.on_event(_event("executing", node_id="a", started_at_ms=ORIGIN))
    hub.on_event(
        _event("node_success", node_id="a", started_at_ms=ORIGIN, finished_at_ms=ORIGIN + 5)
    )
    hub.on_event(_event("executing", node_id="b", started_at_ms=ORIGIN + 5))
    hub.on_event(_event("executing", node_id="c", started_at_ms=ORIGIN + 5))

    (live,) = hub.live_executions()
    assert live.prompt_id == "p1"
    assert live.flow_name == "Demo"
    assert live.flow_id == "flow-1"
    assert live.status == BAR_RUNNING
    assert sorted(live.current_nodes) == ["b", "c"]
    assert live.node_total == 3
    assert live.node_done == 1
    assert live.queue_position is None
    assert live.to_dict()["node_done"] == 1


def test_hub_reports_a_blocked_execution_as_waiting_on_a_human() -> None:
    hub = ObservabilityHub()
    hub.on_event(_event("execution_start", started_at_ms=ORIGIN))
    hub.on_event(_event("executing", node_id="review", started_at_ms=ORIGIN))
    hub.on_event(_event("node_blocked", node_id="review", started_at_ms=ORIGIN))
    (live,) = hub.live_executions()
    assert live.status == "waiting_human"
    assert live.current_nodes == ["review"]


def test_hub_reports_a_started_but_idle_execution_as_queued() -> None:
    hub = ObservabilityHub()
    hub.on_event(_event("execution_start", started_at_ms=ORIGIN))
    (live,) = hub.live_executions()
    assert live.status == "queued"
    assert live.current_nodes == []


def test_finished_executions_leave_the_wall_but_keep_their_chart() -> None:
    hub = ObservabilityHub()
    hub.on_event(_event("execution_start", started_at_ms=ORIGIN))
    hub.on_event(_event("executing", node_id="a", started_at_ms=ORIGIN))
    hub.on_event(_event("execution_success", finished_at_ms=ORIGIN + 100))
    assert hub.live_executions() == []
    chart = hub.gantt("p1")
    assert chart is not None
    assert chart.finished_at_ms == ORIGIN + 100
    assert all(not b.open for b in chart.bars)


def test_hub_profile_matches_a_direct_build() -> None:
    hub = ObservabilityHub()
    hub.on_event(_event("execution_start", started_at_ms=ORIGIN))
    hub.on_event(_event("executing", node_id="a", started_at_ms=ORIGIN))
    hub.on_event(
        _event(
            "node_success", node_id="a", started_at_ms=ORIGIN, finished_at_ms=ORIGIN + 400
        )
    )
    hub.on_event(_event("execution_success", finished_at_ms=ORIGIN + 400))

    profile = hub.profile("p1")
    chart = hub.gantt("p1")
    assert profile is not None and chart is not None
    assert profile.to_dict() == build_profile(chart).to_dict()
    assert profile.total_ms == 400
    assert hub.profile("nope") is None


@pytest.mark.parametrize(
    "event",
    [
        None,
        object(),
        {},
        {"nope": 1},
        "not-an-event",
    ],
)
def test_hub_ignores_events_without_a_prompt_id(event: object) -> None:
    hub = ObservabilityHub()
    hub.on_event(event)
    assert hub.tracked_prompt_ids() == []


def test_hub_swallows_a_failure_in_chart_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The compatibility invariant: a Gantt bug must never surface upstream."""

    def _boom(self, event):  # noqa: ANN001, ANN202
        raise RuntimeError("gantt exploded")

    monkeypatch.setattr(GanttBuilder, "apply", _boom)
    hub = ObservabilityHub()
    hub.on_event(_event("execution_start", started_at_ms=ORIGIN))  # must not raise
    assert hub.gantt("p1") is not None  # builder exists, just empty


def test_eviction_prefers_finished_charts() -> None:
    hub = ObservabilityHub(max_tracked=2)
    hub.on_event(_event("execution_start", prompt_id="done", started_at_ms=ORIGIN))
    hub.on_event(_event("execution_success", prompt_id="done", finished_at_ms=ORIGIN))
    hub.on_event(_event("execution_start", prompt_id="live", started_at_ms=ORIGIN))
    hub.on_event(_event("execution_start", prompt_id="new", started_at_ms=ORIGIN))
    assert sorted(hub.tracked_prompt_ids()) == ["live", "new"]


def test_eviction_falls_back_to_the_oldest_when_all_are_live() -> None:
    hub = ObservabilityHub(max_tracked=2)
    for pid in ("a", "b", "c"):
        hub.on_event(_event("execution_start", prompt_id=pid, started_at_ms=ORIGIN))
    assert sorted(hub.tracked_prompt_ids()) == ["b", "c"]


def test_gc_trims_finished_charts_and_spares_live_ones() -> None:
    hub = ObservabilityHub(max_tracked=64)
    for pid in ("f1", "f2", "f3"):
        hub.on_event(_event("execution_start", prompt_id=pid, started_at_ms=ORIGIN))
        hub.on_event(_event("execution_success", prompt_id=pid, finished_at_ms=ORIGIN))
    hub.on_event(_event("execution_start", prompt_id="live", started_at_ms=ORIGIN))

    assert hub.gc(keep_finished=1) == 2
    remaining = hub.tracked_prompt_ids()
    assert "live" in remaining
    assert len([p for p in remaining if p.startswith("f")]) == 1
    assert hub.gc(keep_finished=1) == 0  # idempotent


def test_forget_drops_a_single_chart() -> None:
    hub = ObservabilityHub()
    hub.on_event(_event("execution_start", started_at_ms=ORIGIN))
    hub.forget("p1")
    assert hub.tracked_prompt_ids() == []
    hub.forget("p1")  # tolerates a missing id


def test_get_observability_hub_is_a_singleton() -> None:
    assert get_observability_hub() is get_observability_hub()


# ---------------------------------------------------------------------------
# Engine integration
# ---------------------------------------------------------------------------


class _ObsSource(WorkflowNode):
    @classmethod
    def define_schema(cls) -> Schema:
        return Schema(
            node_id="_ObsSource",
            display_name="Obs source",
            category="test",
            inputs=[],
            outputs=[IO.String.Output(id="seed")],
            not_idempotent=True,
        )

    async def execute(self, **kwargs) -> NodeOutput:
        return NodeOutput(values=("seed",))


class _ObsWork(WorkflowNode):
    """Sleeps long enough that concurrent branches provably overlap."""

    @classmethod
    def define_schema(cls) -> Schema:
        return Schema(
            node_id="_ObsWork",
            display_name="Obs work",
            category="test",
            inputs=[IO.String.Input(id="value", optional=True)],
            outputs=[IO.String.Output(id="echo")],
            not_idempotent=True,
        )

    async def execute(self, *, value: str | None = None, **kwargs) -> NodeOutput:
        await asyncio.sleep(0.15)
        return NodeOutput(values=(value or "x",))


class _ObsCollect(WorkflowNode):
    @classmethod
    def define_schema(cls) -> Schema:
        return Schema(
            node_id="_ObsCollect",
            display_name="Obs collect",
            category="test",
            inputs=[IO.Array.Input(id="items")],
            outputs=[IO.Array.Output(id="out")],
            not_idempotent=True,
        )

    async def execute(self, *, items=None, **kwargs) -> NodeOutput:
        return NodeOutput(values=(list(items or []),))


@pytest.fixture
async def obs_nodes():
    """Register the test nodes. Explicit (not autouse) so the sync hub tests above
    are not dragged through an async fixture."""
    await bootstrap()
    registry = get_registry()
    for cls in (_ObsSource, _ObsWork, _ObsCollect):
        registry.register(cls)
    yield


def _link(node_id: str, slot: int = 0) -> list:
    return [node_id, slot]


def _parallel_doc(**node_overrides) -> dict:
    nodes = {
        "source": {"class_type": "_ObsSource", "inputs": {}, "control": {}},
        "left": {
            "class_type": "_ObsWork",
            "inputs": {"value": _link("source")},
            "meta": {"name": "Left branch"},
            "control": {},
        },
        "right": {
            "class_type": "_ObsWork",
            "inputs": {"value": _link("source")},
            "control": {},
        },
        "collect": {
            "class_type": "_ObsCollect",
            "inputs": {"items": [_link("left"), _link("right")]},
            "control": {},
        },
    }
    nodes.update(node_overrides)
    return {
        "id": "obs",
        "name": "Observability demo",
        "inputs": [],
        "outputs": [],
        "metadata": {},
        "nodes": nodes,
        "control": {"start": "source", "end": "collect"},
    }


def _persisted_record(result, prompt_id: str) -> dict:
    """Reproduce the persistence round-trip of ``WorkflowService``.

    ``execution_history`` is stored as ``json.dumps([r.model_dump() ...])`` in a
    Text column, so replay only ever sees JSON primitives — the same lossy hop
    is applied here rather than handing the replay live model objects.
    """
    history = json.loads(
        json.dumps([r.model_dump() for r in result.execution_history], default=str)
    )
    return {
        "id": "33333333-3333-3333-3333-333333333333",
        "prompt_id": prompt_id,
        "flow_id": None,
        "flow_name": "Observability demo",
        "status": result.status.value,
        "node_count": len(history),
        "duration_ms": result.duration_ms,
        "started_at": None,
        "completed_at": None,
        "created_at": None,
        "execution_history": history,
    }


@pytest.mark.asyncio
async def test_engine_records_node_spans_in_execution_history(obs_nodes) -> None:
    hub = ObservabilityHub()
    executor = WorkflowExecutor(
        cache_set=build_cache_set("none"),
        max_parallelism=8,
        progress_handlers=[hub.on_event],
    )
    result = await executor.execute(_parallel_doc(), inputs={})
    assert result.status == WorkflowStatus.COMPLETED

    for record in result.execution_history:
        assert record.started_at_ms is not None, record.node_id
        assert record.finished_at_ms is not None, record.node_id
        assert record.finished_at_ms >= record.started_at_ms


@pytest.mark.asyncio
async def test_hub_builds_a_parallel_chart_from_a_real_execution(obs_nodes) -> None:
    hub = ObservabilityHub()
    executor = WorkflowExecutor(
        cache_set=build_cache_set("none"),
        max_parallelism=8,
        progress_handlers=[hub.on_event],
    )
    result = await executor.execute(_parallel_doc(), inputs={})
    prompt_id = result.metadata["prompt_id"]

    chart = hub.gantt(prompt_id)
    assert chart is not None
    assert chart.flow_name == "Observability demo"
    assert {b.node_id for b in chart.bars} == {"source", "left", "right", "collect"}
    assert chart.node_total == 4
    assert chart.status == "success"
    assert chart.finished_at_ms is not None
    assert all(not b.open for b in chart.bars)
    # The editor-assigned label rides the event stream, no flow reload needed.
    left = next(b for b in chart.bars if b.node_id == "left")
    assert left.display_name == "Left branch"
    assert left.class_type == "_ObsWork"
    # The two work nodes overlap, so the chart needs a second row.
    assert chart.lane_count >= 2
    assert hub.live_executions() == []


@pytest.mark.asyncio
async def test_profile_of_a_real_parallel_execution_reports_overlap(obs_nodes) -> None:
    hub = ObservabilityHub()
    executor = WorkflowExecutor(
        cache_set=build_cache_set("none"),
        max_parallelism=8,
        progress_handlers=[hub.on_event],
    )
    result = await executor.execute(_parallel_doc(), inputs={})
    profile = hub.profile(result.metadata["prompt_id"])

    assert profile is not None
    assert profile.approximate is False
    assert profile.critical_path_ms > 0
    assert profile.bottleneck is not None
    assert profile.bottleneck.node_id in ("left", "right")
    # Two ~150ms branches inside one ~150ms window.
    assert profile.parallelism > 1.0
    assert profile.waiting_human_ms == 0
    assert profile.cached_saved_ms == 0


@pytest.mark.asyncio
async def test_replayed_chart_agrees_with_the_live_one(obs_nodes) -> None:
    hub = ObservabilityHub()
    executor = WorkflowExecutor(
        cache_set=build_cache_set("none"),
        max_parallelism=8,
        progress_handlers=[hub.on_event],
    )
    result = await executor.execute(_parallel_doc(), inputs={})
    prompt_id = result.metadata["prompt_id"]
    live = hub.gantt(prompt_id)
    replayed = GanttBuilder.from_execution_history(_persisted_record(result, prompt_id))

    assert live is not None
    assert replayed.approximate is False, "recorded spans must replay exactly"
    assert {b.node_id for b in replayed.bars} == {b.node_id for b in live.bars}
    live_spans = {b.node_id: (b.started_at_ms, b.finished_at_ms) for b in live.bars}
    for bar in replayed.bars:
        assert (bar.started_at_ms, bar.finished_at_ms) == live_spans[bar.node_id]
    assert replayed.lane_count == live.lane_count


@pytest.mark.asyncio
async def test_muted_node_is_replayable_as_a_skipped_bar(obs_nodes) -> None:
    """``_annotate_outcome`` is the only bridge from a live skip to a replayed one."""
    hub = ObservabilityHub()
    executor = WorkflowExecutor(
        cache_set=build_cache_set("none"),
        max_parallelism=8,
        progress_handlers=[hub.on_event],
    )
    doc = _parallel_doc(
        right={
            "class_type": "_ObsWork",
            "inputs": {"value": _link("source")},
            "meta": {"mode": "mute"},
            "control": {},
        }
    )
    result = await executor.execute(doc, inputs={})
    assert result.status == WorkflowStatus.COMPLETED
    prompt_id = result.metadata["prompt_id"]

    live = hub.gantt(prompt_id)
    assert live is not None
    live_right = next(b for b in live.bars if b.node_id == "right")
    assert live_right.status == BAR_SKIPPED

    persisted = next(r for r in result.execution_history if r.node_id == "right")
    assert persisted.metadata.get("skipped_mode") == "mute"

    replayed = GanttBuilder.from_execution_history(_persisted_record(result, prompt_id))
    replayed_right = next(b for b in replayed.bars if b.node_id == "right")
    assert replayed_right.status == BAR_SKIPPED
    assert replayed_right.detail["mode"] == "mute"


@pytest.mark.asyncio
async def test_a_broken_observability_consumer_cannot_fail_an_execution(
    obs_nodes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The zero-intrusion gate, asserted rather than assumed."""

    def _boom(self, event):  # noqa: ANN001, ANN202
        raise RuntimeError("gantt exploded")

    monkeypatch.setattr(GanttBuilder, "apply", _boom)
    hub = ObservabilityHub()
    executor = WorkflowExecutor(
        cache_set=build_cache_set("none"),
        max_parallelism=8,
        progress_handlers=[hub.on_event],
    )
    result = await executor.execute(_parallel_doc(), inputs={})
    assert result.status == WorkflowStatus.COMPLETED


def test_blocked_bar_stays_open_while_a_human_is_asked() -> None:
    """A run parked on a review must show an open bar, not a finished one."""
    hub = ObservabilityHub()
    hub.on_event(_event("execution_start", started_at_ms=ORIGIN, node_total=1))
    hub.on_event(_event("executing", node_id="review", started_at_ms=ORIGIN))
    hub.on_event(_event("node_blocked", node_id="review", started_at_ms=ORIGIN))
    hub.on_event(_event("execution_blocked", node_id="review", tag="approval"))

    chart = hub.gantt("p1")
    assert chart is not None
    assert chart.finished_at_ms is None, "execution_blocked must not end the chart"
    (bar,) = chart.bars
    assert bar.status == BAR_BLOCKED
    assert bar.open is True
    profile = build_profile(chart, now=ORIGIN + 5_000)
    assert profile.waiting_human_ms == 5_000
