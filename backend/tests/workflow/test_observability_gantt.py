"""Unit tests for the Gantt data foundation of the observability plane.

Three concerns, kept separate:

* ``ProgressRegistry`` stamps ``started_at_ms``/``finished_at_ms`` correctly —
  a retry must not reset a span, a loop re-entry must, and a blocked node must
  leave its span open.
* ``GanttBuilder.apply`` maps each event type onto the right bar, idempotently.
* ``GanttBuilder.from_execution_history`` replays a persisted row, exactly when
  timestamps were recorded and approximately when they were not.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from StAgent.workflow.engine import progress as progress_module
from StAgent.workflow.engine.progress import (
    NodeProgressState,
    NodeStatus,
    ProgressEvent,
    ProgressRegistry,
)
from StAgent.workflow.observability.gantt import (
    BAR_BLOCKED,
    BAR_CACHED,
    BAR_ERROR,
    BAR_RUNNING,
    BAR_SKIPPED,
    BAR_SUCCESS,
    ExecutionGantt,
    GanttBar,
    GanttBuilder,
)

ORIGIN = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)


class _Clock:
    """Monotonic fake clock so span arithmetic is exact instead of racy."""

    def __init__(self, start: int = 1_000_000, step: int = 10) -> None:
        self.value = start
        self.step = step

    def __call__(self) -> int:
        self.value += self.step
        return self.value


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    """Pin ``progress.now_ms`` (the registry's clock) to a fake."""
    fake = _Clock()
    monkeypatch.setattr(progress_module, "now_ms", fake)
    return fake


def _state(
    node_id: str,
    *,
    started: int | None = None,
    finished: int | None = None,
    metadata: dict | None = None,
    error: str | None = None,
) -> NodeProgressState:
    return NodeProgressState(
        node_id=node_id,
        started_at_ms=started,
        finished_at_ms=finished,
        metadata=dict(metadata or {}),
        error=error,
    )


def _event(
    etype: str,
    *,
    node_id: str | None = None,
    state: NodeProgressState | None = None,
    data: dict | None = None,
    prompt_id: str = "p1",
) -> ProgressEvent:
    return ProgressEvent(
        type=etype,
        prompt_id=prompt_id,
        node_id=node_id,
        state=state,
        data=data or {},
    )


def _start_event(**overrides) -> ProgressEvent:
    data = {
        "workflow_id": "flow-1",
        "flow_name": "Demo flow",
        "started_at_ms": ORIGIN,
        "node_total": 2,
        "nodes": {
            "n": {"class_type": "TransformNode", "display_name": "Shape it"},
        },
    }
    data.update(overrides)
    return _event("execution_start", data=data)


# ---------------------------------------------------------------------------
# ProgressRegistry span stamping
# ---------------------------------------------------------------------------


def test_running_opens_a_span_and_retry_does_not_reset_it(clock: _Clock) -> None:
    registry = ProgressRegistry(prompt_id="p1")
    registry.set_status("n", NodeStatus.RUNNING)
    opened = registry.state("n").started_at_ms
    assert opened is not None
    assert registry.state("n").finished_at_ms is None

    # The runner re-emits RUNNING for each retry attempt: same span, new label.
    registry.set_status("n", NodeStatus.RUNNING, metadata={"retry": 1})
    assert registry.state("n").started_at_ms == opened


def test_terminal_status_closes_the_span(clock: _Clock) -> None:
    registry = ProgressRegistry(prompt_id="p1")
    registry.set_status("n", NodeStatus.RUNNING)
    opened = registry.state("n").started_at_ms
    registry.set_status("n", NodeStatus.SUCCESS)
    state = registry.state("n")
    assert state.started_at_ms == opened
    assert state.finished_at_ms == clock.value


def test_loop_reentry_opens_a_fresh_span(clock: _Clock) -> None:
    registry = ProgressRegistry(prompt_id="p1")
    registry.set_status("n", NodeStatus.RUNNING)
    first_start = registry.state("n").started_at_ms
    registry.set_status("n", NodeStatus.SUCCESS)
    registry.set_status("n", NodeStatus.RUNNING)
    state = registry.state("n")
    assert state.started_at_ms is not None
    assert state.started_at_ms > (first_start or 0)
    assert state.finished_at_ms is None


def test_blocked_leaves_the_span_open(clock: _Clock) -> None:
    registry = ProgressRegistry(prompt_id="p1")
    registry.set_status("n", NodeStatus.RUNNING)
    registry.set_status("n", NodeStatus.BLOCKED)
    state = registry.state("n")
    assert state.started_at_ms is not None
    assert state.finished_at_ms is None


@pytest.mark.parametrize("status", [NodeStatus.CACHED, NodeStatus.SKIPPED])
def test_cache_hit_and_skip_get_a_span_without_ever_running(
    clock: _Clock, status: NodeStatus
) -> None:
    registry = ProgressRegistry(prompt_id="p1")
    registry.set_status("n", status)
    state = registry.state("n")
    assert state.started_at_ms is not None
    assert state.finished_at_ms == state.started_at_ms


# ---------------------------------------------------------------------------
# GanttBuilder: event -> bar mapping
# ---------------------------------------------------------------------------


def test_execution_start_seeds_the_chart_header() -> None:
    builder = GanttBuilder("p1")
    builder.apply(_start_event())
    chart = builder.snapshot()
    assert chart.prompt_id == "p1"
    assert chart.flow_name == "Demo flow"
    assert chart.flow_id == "flow-1"
    assert chart.started_at_ms == ORIGIN
    assert chart.node_total == 2
    assert chart.status == BAR_RUNNING
    assert chart.finished_at_ms is None


@pytest.mark.parametrize(
    ("event_type", "expected_status", "expected_cached", "expect_open"),
    [
        ("executing", BAR_RUNNING, False, True),
        ("node_cached", BAR_CACHED, True, False),
        ("node_skipped", BAR_SKIPPED, False, False),
        ("node_blocked", BAR_BLOCKED, False, True),
    ],
)
def test_event_opens_the_expected_bar(
    event_type: str, expected_status: str, expected_cached: bool, expect_open: bool
) -> None:
    builder = GanttBuilder("p1")
    builder.apply(_start_event())
    builder.apply(
        _event(
            event_type,
            node_id="n",
            state=_state("n", started=ORIGIN + 100, finished=ORIGIN + 150),
        )
    )
    (bar,) = builder.snapshot().bars
    assert bar.status == expected_status
    assert bar.cached is expected_cached
    assert bar.open is expect_open
    assert bar.started_at_ms == ORIGIN + 100
    # The catalogue from ``execution_start`` labels the bar with no extra lookup.
    assert bar.class_type == "TransformNode"
    assert bar.display_name == "Shape it"


@pytest.mark.parametrize(
    ("close_type", "expected_status"),
    [
        ("node_success", BAR_SUCCESS),
        ("node_error", BAR_ERROR),
        # ``executed`` is a fallback for a dropped close event.
        ("executed", BAR_SUCCESS),
    ],
)
def test_close_event_closes_the_open_bar(close_type: str, expected_status: str) -> None:
    builder = GanttBuilder("p1")
    builder.apply(_event("executing", node_id="n", state=_state("n", started=ORIGIN)))
    builder.apply(
        _event(
            close_type,
            node_id="n",
            state=_state("n", started=ORIGIN, finished=ORIGIN + 400, error="boom"),
        )
    )
    (bar,) = builder.snapshot().bars
    assert bar.status == expected_status
    assert bar.finished_at_ms == ORIGIN + 400
    assert bar.duration_ms == 400
    assert bar.open is False


def test_error_detail_carries_the_message() -> None:
    builder = GanttBuilder("p1")
    builder.apply(_event("executing", node_id="n", state=_state("n", started=ORIGIN)))
    builder.apply(
        _event(
            "node_error",
            node_id="n",
            state=_state("n", started=ORIGIN, finished=ORIGIN + 5, error="boom"),
        )
    )
    (bar,) = builder.snapshot().bars
    assert bar.detail["error"] == "boom"


def test_duplicate_close_event_is_a_noop() -> None:
    builder = GanttBuilder("p1")
    builder.apply(_event("executing", node_id="n", state=_state("n", started=ORIGIN)))
    close = _event(
        "node_success", node_id="n", state=_state("n", started=ORIGIN, finished=ORIGIN + 400)
    )
    builder.apply(close)
    builder.apply(close)
    builder.apply(
        _event(
            "node_success",
            node_id="n",
            state=_state("n", started=ORIGIN, finished=ORIGIN + 9999),
        )
    )
    bars = builder.snapshot().bars
    assert len(bars) == 1
    assert bars[0].finished_at_ms == ORIGIN + 400


def test_retry_relabels_the_open_bar_instead_of_splitting_it() -> None:
    builder = GanttBuilder("p1")
    builder.apply(_event("executing", node_id="n", state=_state("n", started=ORIGIN)))
    builder.apply(
        _event(
            "executing",
            node_id="n",
            state=_state("n", started=ORIGIN, metadata={"retry": 1, "max_retries": 3}),
        )
    )
    # Re-delivering the same retry event must not bump the counter.
    builder.apply(
        _event(
            "executing",
            node_id="n",
            state=_state("n", started=ORIGIN, metadata={"retry": 1, "max_retries": 3}),
        )
    )
    bars = builder.snapshot().bars
    assert len(bars) == 1
    assert bars[0].detail["retry"] == 1
    assert bars[0].detail["max_retries"] == 3


def test_loop_reentry_opens_a_second_bar() -> None:
    builder = GanttBuilder("p1")
    builder.apply(_event("executing", node_id="n", state=_state("n", started=ORIGIN)))
    builder.apply(
        _event(
            "node_success",
            node_id="n",
            state=_state("n", started=ORIGIN, finished=ORIGIN + 100),
        )
    )
    builder.apply(
        _event("executing", node_id="n", state=_state("n", started=ORIGIN + 200))
    )
    builder.apply(
        _event(
            "node_success",
            node_id="n",
            state=_state("n", started=ORIGIN + 200, finished=ORIGIN + 300),
        )
    )
    bars = builder.snapshot().bars
    assert [b.iteration for b in bars] == [0, 1]
    assert [b.node_id for b in bars] == ["n", "n"]
    assert [b.duration_ms for b in bars] == [100, 100]


def test_new_span_while_open_closes_the_previous_bar() -> None:
    """A dropped close event must not leave two open bars for one node."""
    builder = GanttBuilder("p1")
    builder.apply(_event("executing", node_id="n", state=_state("n", started=ORIGIN)))
    builder.apply(
        _event("executing", node_id="n", state=_state("n", started=ORIGIN + 500))
    )
    bars = builder.snapshot().bars
    assert len(bars) == 2
    assert bars[0].finished_at_ms == ORIGIN + 500
    assert bars[1].open is True


def test_blocked_bar_keeps_its_identity_when_resumed() -> None:
    builder = GanttBuilder("p1")
    builder.apply(_event("executing", node_id="n", state=_state("n", started=ORIGIN)))
    builder.apply(
        _event(
            "node_blocked",
            node_id="n",
            state=_state("n", started=ORIGIN, metadata={"duration_ms": 40}),
        )
    )
    blocked = builder.snapshot().bars[0]
    assert blocked.open is True
    assert blocked.detail["compute_ms"] == 40

    builder.apply(
        _event(
            "node_success",
            node_id="n",
            state=_state("n", started=ORIGIN, finished=ORIGIN + 5000),
        )
    )
    (bar,) = builder.snapshot().bars
    assert bar.status == BAR_SUCCESS
    assert bar.detail["resumed"] is True
    assert bar.duration_ms == 5000


def test_execution_blocked_does_not_end_the_chart() -> None:
    builder = GanttBuilder("p1")
    builder.apply(_start_event())
    builder.apply(_event("execution_blocked", node_id="n", data={"tag": "review"}))
    assert builder.finished is False
    assert builder.snapshot().finished_at_ms is None


@pytest.mark.parametrize(
    ("etype", "expected_status"),
    [
        ("execution_success", "success"),
        ("execution_failed", "failed"),
        ("execution_timeout", "timeout"),
    ],
)
def test_terminal_execution_event_clamps_open_bars(
    etype: str, expected_status: str
) -> None:
    builder = GanttBuilder("p1")
    builder.apply(_start_event())
    builder.apply(_event("executing", node_id="n", state=_state("n", started=ORIGIN)))
    builder.apply(_event(etype, data={"finished_at_ms": ORIGIN + 5000}))
    chart = builder.snapshot()
    assert builder.finished is True
    assert chart.status == expected_status
    assert chart.finished_at_ms == ORIGIN + 5000
    assert all(not b.open for b in chart.bars)
    assert chart.bars[0].finished_at_ms == ORIGIN + 5000


def test_progress_event_annotates_the_open_bar() -> None:
    builder = GanttBuilder("p1")
    builder.apply(_event("executing", node_id="n", state=_state("n", started=ORIGIN)))
    state = _state("n", started=ORIGIN)
    state.value, state.max = 3.0, 10.0
    builder.apply(_event("progress", node_id="n", state=state))
    (bar,) = builder.snapshot().bars
    assert bar.detail["value"] == 3.0
    assert bar.detail["max"] == 10.0


def test_events_are_accepted_as_plain_dicts() -> None:
    """WebSocket-shaped payloads must fold in like in-process dataclasses."""
    builder = GanttBuilder("p1")
    builder.apply({"type": "execution_start", "data": {"started_at_ms": ORIGIN}})
    builder.apply(
        {
            "type": "executing",
            "node_id": "n",
            "state": {"started_at_ms": ORIGIN + 10, "finished_at_ms": None},
        }
    )
    builder.apply(
        {
            "type": "node_success",
            "node_id": "n",
            "state": {"started_at_ms": ORIGIN + 10, "finished_at_ms": ORIGIN + 60},
        }
    )
    (bar,) = builder.snapshot().bars
    assert bar.status == BAR_SUCCESS
    assert bar.duration_ms == 50


def test_unknown_and_nodeless_events_are_ignored() -> None:
    builder = GanttBuilder("p1")
    builder.apply(_event("", node_id="n"))
    builder.apply(_event("something_else", node_id="n"))
    builder.apply(_event("node_success"))  # no node_id
    assert builder.snapshot().bars == []


def test_snapshot_is_a_deep_copy() -> None:
    builder = GanttBuilder("p1")
    builder.apply(_event("executing", node_id="n", state=_state("n", started=ORIGIN)))
    snapshot = builder.snapshot()
    snapshot.bars[0].detail["mutated"] = True
    snapshot.bars[0].status = BAR_ERROR
    again = builder.snapshot()
    assert "mutated" not in again.bars[0].detail
    assert again.bars[0].status == BAR_RUNNING


def test_to_dict_is_wire_ready() -> None:
    builder = GanttBuilder("p1")
    builder.apply(_start_event())
    builder.apply(_event("executing", node_id="n", state=_state("n", started=ORIGIN)))
    payload = builder.snapshot().to_dict()
    assert payload["prompt_id"] == "p1"
    assert payload["approximate"] is False
    assert payload["lane_count"] == 1
    assert payload["server_now_ms"] > 0
    (bar,) = payload["bars"]
    assert bar["display_name"] == "Shape it"
    assert bar["finished_at_ms"] is None
    assert bar["duration_ms"] == 0


# ---------------------------------------------------------------------------
# Lane assignment (greedy interval colouring)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("spans", "expected_lanes", "expected_lane_count"),
    [
        # A serial chain collapses onto a single row.
        ([(0, 100), (100, 200), (200, 300)], [0, 0, 0], 1),
        # Two overlapping branches need two rows.
        ([(0, 100), (50, 150)], [0, 1], 2),
        # A three-way fan-out staggers into three rows.
        ([(0, 100), (10, 110), (20, 120)], [0, 1, 2], 3),
        # An open bar holds its lane indefinitely.
        ([(0, None), (10, 50)], [0, 1], 2),
        # A later bar reuses the lane freed by the first.
        ([(0, 100), (0, 100), (100, 200)], [0, 1, 0], 2),
        # Touching bars do not overlap.
        ([(0, 100), (100, 200)], [0, 0], 1),
    ],
)
def test_lane_assignment(
    spans: list[tuple[int, int | None]],
    expected_lanes: list[int],
    expected_lane_count: int,
) -> None:
    chart = ExecutionGantt(prompt_id="p1", started_at_ms=0)
    for index, (start, stop) in enumerate(spans):
        chart.bars.append(
            GanttBar(node_id=f"n{index}", started_at_ms=start, finished_at_ms=stop)
        )
    chart.assign_lanes()
    lanes = [b.lane for b in sorted(chart.bars, key=lambda b: b.node_id)]
    assert lanes == expected_lanes
    assert chart.lane_count == expected_lane_count


def test_empty_chart_still_reports_one_lane() -> None:
    chart = ExecutionGantt(prompt_id="p1")
    chart.assign_lanes()
    assert chart.lane_count == 1


# ---------------------------------------------------------------------------
# Replay from execution_history
# ---------------------------------------------------------------------------


def _record(history: list[dict], **overrides) -> dict:
    record = {
        "id": "11111111-1111-1111-1111-111111111111",
        "prompt_id": "p1",
        "flow_id": "22222222-2222-2222-2222-222222222222",
        "flow_name": "Demo flow",
        "status": "completed",
        "node_count": len(history),
        "duration_ms": 2000,
        "started_at": "2026-01-01T00:00:00+00:00",
        "completed_at": "2026-01-01T00:00:02+00:00",
        "execution_history": history,
    }
    record.update(overrides)
    return record


def test_replay_uses_recorded_timestamps_exactly() -> None:
    chart = GanttBuilder.from_execution_history(
        _record(
            [
                {
                    "node_id": "a",
                    "status": "completed",
                    "duration_ms": 500,
                    "started_at_ms": ORIGIN,
                    "finished_at_ms": ORIGIN + 500,
                    "metadata": {"class_type": "TransformNode"},
                },
                {
                    "node_id": "b",
                    "status": "completed",
                    "duration_ms": 300,
                    "started_at_ms": ORIGIN + 200,
                    "finished_at_ms": ORIGIN + 500,
                    "metadata": {},
                },
            ]
        )
    )
    assert chart.approximate is False
    assert chart.prompt_id == "p1"
    assert chart.execution_id == "11111111-1111-1111-1111-111111111111"
    assert chart.flow_name == "Demo flow"
    assert chart.started_at_ms == ORIGIN
    assert chart.finished_at_ms == ORIGIN + 2000
    by_node = {b.node_id: b for b in chart.bars}
    assert by_node["a"].started_at_ms == ORIGIN
    assert by_node["a"].duration_ms == 500
    assert by_node["a"].class_type == "TransformNode"
    assert by_node["b"].started_at_ms == ORIGIN + 200
    # The two spans overlap, so the chart shows them as parallel rows.
    assert chart.lane_count == 2


def test_replay_without_timestamps_is_flagged_approximate() -> None:
    chart = GanttBuilder.from_execution_history(
        _record(
            [
                {"node_id": "a", "status": "completed", "duration_ms": 500},
                {"node_id": "b", "status": "completed", "duration_ms": 300},
            ]
        )
    )
    assert chart.approximate is True
    # Stitched end-to-end from duration_ms, so it reads as a serial chain.
    assert chart.lane_count == 1
    by_node = {b.node_id: b for b in chart.bars}
    assert by_node["a"].started_at_ms == ORIGIN
    assert by_node["a"].finished_at_ms == ORIGIN + 500
    assert by_node["b"].started_at_ms == ORIGIN + 500
    assert by_node["b"].finished_at_ms == ORIGIN + 800


def test_replay_mixes_precise_and_missing_timestamps() -> None:
    """One legacy entry is enough to mark the whole chart approximate."""
    chart = GanttBuilder.from_execution_history(
        _record(
            [
                {
                    "node_id": "a",
                    "status": "completed",
                    "duration_ms": 500,
                    "started_at_ms": ORIGIN,
                    "finished_at_ms": ORIGIN + 500,
                },
                {"node_id": "b", "status": "completed", "duration_ms": 300},
            ]
        )
    )
    assert chart.approximate is True
    by_node = {b.node_id: b for b in chart.bars}
    assert by_node["b"].started_at_ms == ORIGIN + 500


@pytest.mark.parametrize(
    ("status", "metadata", "expected_status", "expected_cached"),
    [
        ("completed", {}, BAR_SUCCESS, False),
        ("failed", {}, BAR_ERROR, False),
        ("timeout", {}, BAR_ERROR, False),
        ("waiting_human", {}, BAR_BLOCKED, False),
        # Cache/skip provenance is stamped by ``_annotate_outcome`` at write time,
        # which is the only way a replay can tell these from a plain success.
        ("completed", {"cached": True}, BAR_CACHED, True),
        ("completed", {"skipped_mode": "mute"}, BAR_SKIPPED, False),
        ("completed", {"skipped_mode": "bypass"}, BAR_SKIPPED, False),
    ],
)
def test_replay_status_colouring(
    status: str, metadata: dict, expected_status: str, expected_cached: bool
) -> None:
    chart = GanttBuilder.from_execution_history(
        _record(
            [
                {
                    "node_id": "a",
                    "status": status,
                    "duration_ms": 0,
                    "started_at_ms": ORIGIN,
                    "finished_at_ms": ORIGIN,
                    "metadata": metadata,
                }
            ]
        )
    )
    (bar,) = chart.bars
    assert bar.status == expected_status
    assert bar.cached is expected_cached


def test_replay_gives_each_repeat_of_a_node_its_own_iteration() -> None:
    chart = GanttBuilder.from_execution_history(
        _record(
            [
                {
                    "node_id": "loop",
                    "status": "completed",
                    "duration_ms": 100,
                    "started_at_ms": ORIGIN,
                    "finished_at_ms": ORIGIN + 100,
                },
                {
                    "node_id": "loop",
                    "status": "completed",
                    "duration_ms": 100,
                    "started_at_ms": ORIGIN + 200,
                    "finished_at_ms": ORIGIN + 300,
                },
            ]
        )
    )
    assert [b.iteration for b in chart.bars] == [0, 1]
    assert chart.lane_count == 1


def test_replay_carries_error_and_retry_detail() -> None:
    chart = GanttBuilder.from_execution_history(
        _record(
            [
                {
                    "node_id": "a",
                    "status": "failed",
                    "duration_ms": 250,
                    "error": "ConnectionError: nope",
                    "started_at_ms": ORIGIN,
                    "finished_at_ms": ORIGIN + 250,
                    "metadata": {"attempts": 3},
                }
            ]
        )
    )
    (bar,) = chart.bars
    assert bar.detail["error"] == "ConnectionError: nope"
    assert bar.detail["retry"] == 3
    assert bar.detail["compute_ms"] == 250


def test_replay_tolerates_a_malformed_history() -> None:
    chart = GanttBuilder.from_execution_history(
        _record(["not-a-dict", {"no_node_id": True}, None])  # type: ignore[list-item]
    )
    assert chart.bars == []
    assert chart.started_at_ms == ORIGIN


def test_replay_of_a_still_running_row_leaves_the_chart_open() -> None:
    chart = GanttBuilder.from_execution_history(
        _record(
            [
                {
                    "node_id": "a",
                    "status": "completed",
                    "duration_ms": 100,
                    "started_at_ms": ORIGIN,
                    "finished_at_ms": ORIGIN + 100,
                }
            ],
            status="running",
            completed_at=None,
        )
    )
    assert chart.finished_at_ms is None
    assert chart.status == "running"


def test_replay_falls_back_to_the_earliest_recorded_stamp_for_the_origin() -> None:
    chart = GanttBuilder.from_execution_history(
        _record(
            [
                {
                    "node_id": "a",
                    "status": "completed",
                    "duration_ms": 100,
                    "started_at_ms": ORIGIN + 700,
                    "finished_at_ms": ORIGIN + 800,
                }
            ],
            started_at=None,
            created_at=None,
        )
    )
    assert chart.started_at_ms == ORIGIN + 700
