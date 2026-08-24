"""Unit tests for :func:`StAgent.workflow.observability.build_profile`.

Every number in an :class:`ExecutionProfile` is a descriptive statistic over bar
intervals, so these are table-driven arithmetic tests over hand-built charts —
no engine, no clock, no I/O.
"""

from __future__ import annotations

import pytest

from StAgent.workflow.observability.gantt import (
    BAR_BLOCKED,
    BAR_CACHED,
    BAR_ERROR,
    BAR_SUCCESS,
    ExecutionGantt,
    GanttBar,
)
from StAgent.workflow.observability.profile import (
    KIND_CACHED_SAVING,
    KIND_COMPUTE,
    KIND_RETRY,
    KIND_WAITING_HUMAN,
    build_profile,
)

NOW = 10_000


def _bar(
    node_id: str,
    start: int,
    stop: int | None,
    *,
    status: str = BAR_SUCCESS,
    cached: bool = False,
    class_type: str = "",
    detail: dict | None = None,
    iteration: int = 0,
) -> GanttBar:
    return GanttBar(
        node_id=node_id,
        class_type=class_type,
        display_name=node_id,
        started_at_ms=start,
        finished_at_ms=stop,
        status=status,
        cached=cached,
        iteration=iteration,
        detail=dict(detail or {}),
    )


def _chart(bars: list[GanttBar], *, finished: int | None, **kwargs) -> ExecutionGantt:
    chart = ExecutionGantt(
        prompt_id="p1", started_at_ms=0, finished_at_ms=finished, bars=bars, **kwargs
    )
    chart.assign_lanes()
    return chart


# ---------------------------------------------------------------------------
# Core arithmetic
# ---------------------------------------------------------------------------


def test_empty_chart_profiles_to_zero() -> None:
    profile = build_profile(_chart([], finished=300), now=NOW)
    assert profile.total_ms == 300
    assert profile.critical_path_ms == 0
    assert profile.parallelism == 0.0
    assert profile.timeline_efficiency == 0.0
    assert profile.bottleneck is None
    assert profile.shares == []
    assert profile.bar_count == 0


@pytest.mark.parametrize(
    (
        "spans",
        "finished",
        "expected_total",
        "expected_critical_path",
        "expected_parallelism",
        "expected_efficiency",
    ),
    [
        # A serial chain: busy the whole time, strictly sequential.
        ([(0, 100), (100, 300)], 300, 300, 300, 1.0, 1.0),
        # Two nodes fully overlapped: twice the work in the same wall time.
        ([(0, 200), (0, 200)], 200, 200, 200, 2.0, 1.0),
        # Partial overlap: 300ms of work over a 200ms union.
        ([(0, 200), (100, 200)], 200, 200, 200, 1.5, 1.0),
        # A scheduling gap shows up as sub-1.0 efficiency, not as parallelism.
        ([(0, 100), (200, 300)], 300, 300, 200, 1.0, pytest.approx(2 / 3)),
        # Trailing idle time after the last node finished.
        ([(0, 100)], 400, 400, 100, 1.0, 0.25),
    ],
)
def test_profile_arithmetic(
    spans: list[tuple[int, int]],
    finished: int,
    expected_total: int,
    expected_critical_path: int,
    expected_parallelism: float,
    expected_efficiency: float,
) -> None:
    bars = [_bar(f"n{i}", start, stop) for i, (start, stop) in enumerate(spans)]
    profile = build_profile(_chart(bars, finished=finished), now=NOW)
    assert profile.total_ms == expected_total
    assert profile.critical_path_ms == expected_critical_path
    assert profile.parallelism == pytest.approx(expected_parallelism)
    assert profile.timeline_efficiency == expected_efficiency


def test_bottleneck_is_the_longest_compute_bar() -> None:
    profile = build_profile(
        _chart(
            [
                _bar("fast", 0, 50, class_type="A"),
                _bar("slow", 50, 950, class_type="B"),
                _bar("mid", 950, 1200, class_type="C"),
            ],
            finished=1200,
        ),
        now=NOW,
    )
    assert profile.bottleneck is not None
    assert profile.bottleneck.node_id == "slow"
    assert profile.bottleneck.duration_ms == 900
    assert profile.bottleneck.class_type == "B"
    assert profile.bottleneck.kind == KIND_COMPUTE
    # 900 of the 1200ms of node time (50 + 900 + 250).
    assert profile.bottleneck.share == pytest.approx(0.75)


def test_shares_sum_to_one_and_are_sorted_longest_first() -> None:
    profile = build_profile(
        _chart([_bar("a", 0, 300), _bar("b", 300, 400)], finished=400), now=NOW
    )
    assert [s.node_id for s in profile.shares] == ["a", "b"]
    assert sum(s.share for s in profile.shares) == pytest.approx(1.0)


def test_open_bars_are_measured_to_now() -> None:
    """A live chart must profile as work-in-progress, not as zero."""
    profile = build_profile(_chart([_bar("a", 0, None)], finished=None), now=1_500)
    assert profile.total_ms == 1_500
    assert profile.critical_path_ms == 1_500
    assert profile.bottleneck is not None
    assert profile.bottleneck.duration_ms == 1_500


def test_failed_bar_still_counts_as_compute() -> None:
    profile = build_profile(
        _chart([_bar("boom", 0, 400, status=BAR_ERROR)], finished=400), now=NOW
    )
    assert profile.bottleneck is not None
    assert profile.bottleneck.node_id == "boom"
    assert profile.bottleneck.duration_ms == 400


def test_retry_bar_is_labelled_as_such() -> None:
    profile = build_profile(
        _chart([_bar("flaky", 0, 400, detail={"retry": 2})], finished=400), now=NOW
    )
    (share,) = profile.shares
    assert share.kind == KIND_RETRY


def test_iteration_is_carried_onto_the_share() -> None:
    profile = build_profile(
        _chart(
            [
                _bar("loop", 0, 100, iteration=0),
                _bar("loop", 100, 500, iteration=1),
            ],
            finished=500,
        ),
        now=NOW,
    )
    assert profile.bottleneck is not None
    assert profile.bottleneck.iteration == 1


# ---------------------------------------------------------------------------
# Human wait
# ---------------------------------------------------------------------------


def test_live_blocked_bar_splits_into_compute_and_human_wait() -> None:
    """An open blocked bar: everything past its own compute time is a wait."""
    profile = build_profile(
        _chart(
            [_bar("review", 0, None, status=BAR_BLOCKED, detail={"compute_ms": 50})],
            finished=None,
        ),
        now=1_000,
    )
    assert profile.waiting_human_ms == 950
    kinds = {s.kind: s.duration_ms for s in profile.shares}
    assert kinds[KIND_COMPUTE] == 50
    assert kinds[KIND_WAITING_HUMAN] == 950
    # A human wait is not a bottleneck to optimise — the person is.
    assert profile.bottleneck is not None
    assert profile.bottleneck.kind == KIND_COMPUTE


def test_replayed_blocked_bar_charges_the_gap_before_its_successor() -> None:
    """A persisted blocked bar was clamped shut, so the wait is the gap after it."""
    profile = build_profile(
        _chart(
            [
                _bar("review", 0, 100, status=BAR_BLOCKED, detail={"compute_ms": 100}),
                _bar("after", 900, 1_000),
            ],
            finished=1_000,
        ),
        now=NOW,
    )
    assert profile.waiting_human_ms == 800
    kinds = {s.kind: s.duration_ms for s in profile.shares}
    assert kinds[KIND_WAITING_HUMAN] == 800
    assert kinds[KIND_COMPUTE] == 100  # the "after" bar; "review" contributed 0


def test_blocked_bar_with_no_successor_waits_until_the_chart_ends() -> None:
    profile = build_profile(
        _chart(
            [_bar("review", 0, 100, status=BAR_BLOCKED, detail={"compute_ms": 100})],
            finished=600,
        ),
        now=NOW,
    )
    assert profile.waiting_human_ms == 500


def test_human_wait_is_excluded_from_the_critical_path_denominator() -> None:
    """Waiting on a person inflates neither the parallelism nor the bottleneck."""
    profile = build_profile(
        _chart(
            [_bar("review", 0, None, status=BAR_BLOCKED, detail={"compute_ms": 100})],
            finished=None,
        ),
        now=1_100,
    )
    # spent = 100 compute + 1000 wait; critical path is the 1100ms bar itself.
    assert profile.parallelism == pytest.approx(1.0)
    assert profile.timeline_efficiency == 1.0


# ---------------------------------------------------------------------------
# Cache savings
# ---------------------------------------------------------------------------


def test_cache_hit_saving_estimated_from_the_same_node() -> None:
    profile = build_profile(
        _chart(
            [
                _bar("step", 0, 400, iteration=0),
                _bar("step", 400, 400, status=BAR_CACHED, cached=True, iteration=1),
            ],
            finished=400,
        ),
        now=NOW,
    )
    assert profile.cached_saved_ms == 400
    saving = [s for s in profile.shares if s.kind == KIND_CACHED_SAVING]
    assert len(saving) == 1
    assert saving[0].duration_ms == 400
    # A cache hit costs nothing, so it never becomes the bottleneck.
    assert profile.bottleneck is not None
    assert profile.bottleneck.kind == KIND_COMPUTE


def test_cache_hit_saving_falls_back_to_the_same_node_type() -> None:
    profile = build_profile(
        _chart(
            [
                _bar("other", 0, 200, class_type="Embed"),
                _bar("hit", 200, 200, status=BAR_CACHED, cached=True, class_type="Embed"),
            ],
            finished=200,
        ),
        now=NOW,
    )
    assert profile.cached_saved_ms == 200


def test_cache_hit_with_no_evidence_reports_no_saving() -> None:
    """Better a zero than an invented number."""
    profile = build_profile(
        _chart(
            [_bar("hit", 0, 0, status=BAR_CACHED, cached=True)],
            finished=100,
        ),
        now=NOW,
    )
    assert profile.cached_saved_ms == 0
    assert profile.shares == []
    assert profile.bottleneck is None


def test_cache_saving_averages_several_samples() -> None:
    profile = build_profile(
        _chart(
            [
                _bar("step", 0, 100, iteration=0),
                _bar("step", 100, 400, iteration=1),
                _bar("step", 400, 400, status=BAR_CACHED, cached=True, iteration=2),
            ],
            finished=400,
        ),
        now=NOW,
    )
    assert profile.cached_saved_ms == 200  # mean of 100 and 300


# ---------------------------------------------------------------------------
# Wire shape
# ---------------------------------------------------------------------------


def test_approximate_flag_propagates_to_the_profile() -> None:
    chart = _chart([_bar("a", 0, 100)], finished=100, approximate=True)
    payload = build_profile(chart, now=NOW).to_dict()
    assert payload["approximate"] is True
    assert payload["prompt_id"] == "p1"
    assert payload["bottleneck"]["node_id"] == "a"
    assert payload["parallelism"] == 1.0
    assert payload["bar_count"] == 1
    assert isinstance(payload["shares"], list)
