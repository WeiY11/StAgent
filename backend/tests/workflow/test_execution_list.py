"""Unit tests + scaling benchmark for the ``ExecutionList`` scheduler.

The scheduler keeps a reverse dependency index (``state.dependents``:
upstream -> direct downstream ids) so that completing / pruning / reopening a
node touches only its direct dependents instead of scanning every strong link.
These tests pin down the scheduling semantics that the index must preserve,
and the benchmark demonstrates the ``O(N^2) -> O(E)`` improvement on a fan-in
DAG (one merge node with many upstream branches).

Run with::

    uv run pytest tests/workflow/test_execution_list.py -s
"""

from __future__ import annotations

import time

from leagent.workflow.engine.graph import (
    DynamicPrompt,
    ExecutionList,
    TopologicalSort,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _chain_spec(ids: list[str]) -> dict[str, dict]:
    """Linear DAG spec: ids[0] -> ids[1] -> ... via each node's ``input``."""
    nodes: dict[str, dict] = {}
    for i, nid in enumerate(ids):
        inputs: dict[str, list] = {}
        if i > 0:
            inputs["input"] = [ids[i - 1], 0]
        nodes[nid] = {"inputs": inputs}
    return nodes


def _fan_in_spec(branches: list[str], merge: str) -> dict[str, dict]:
    """Fan-in DAG: every branch node feeds the single ``merge`` node."""
    nodes: dict[str, dict] = {b: {"inputs": {}} for b in branches}
    nodes[merge] = {"inputs": {f"in_{i}": [b, 0] for i, b in enumerate(branches)}}
    return nodes


def _build(spec: dict[str, dict], seed: str) -> ExecutionList:
    prompt = DynamicPrompt(spec)
    topo = TopologicalSort(prompt)
    exec_list = ExecutionList(prompt, topo)
    exec_list.add_node(seed)
    return exec_list


# ---------------------------------------------------------------------------
# Scheduling semantics preserved by the reverse index
# ---------------------------------------------------------------------------


class TestReverseIndexPromotion:
    def test_dependents_index_populated(self) -> None:
        el = _build(_chain_spec(["a", "b", "c"]), "c")
        assert el.state.dependents.get("a") == {"b"}
        assert el.state.dependents.get("b") == {"c"}

    def test_chain_becomes_ready_in_order(self) -> None:
        el = _build(_chain_spec(["a", "b", "c"]), "c")
        # Only the source is ready initially.
        assert el.state.ready == {"a"}

        el.state.in_progress.add("a")
        el.complete_node_execution("a")
        assert "b" in el.state.ready

        el.state.in_progress.add("b")
        el.complete_node_execution("b")
        assert "c" in el.state.ready

    def test_fan_in_merge_waits_for_all_branches(self) -> None:
        branches = [f"b{i}" for i in range(5)]
        el = _build(_fan_in_spec(branches, "merge"), "merge")
        # All branches are independent sources → ready together.
        assert el.state.ready == set(branches)

        # Completing all but one branch must NOT release the merge.
        for b in branches[:-1]:
            el.state.in_progress.add(b)
            el.complete_node_execution(b)
        assert "merge" not in el.state.ready

        # The last branch releases it.
        el.state.in_progress.add(branches[-1])
        el.complete_node_execution(branches[-1])
        assert "merge" in el.state.ready

    def test_completed_dependent_not_repromoted(self) -> None:
        el = _build(_chain_spec(["a", "b"]), "b")
        el.state.in_progress.add("a")
        el.complete_node_execution("a")
        assert "b" in el.state.ready
        # Run and complete b, then re-complete a (idempotent) — b stays put.
        el.state.in_progress.add("b")
        el.complete_node_execution("b")
        el.complete_node_execution("a")
        assert "b" in el.state.completed


class TestReverseIndexPruning:
    def test_pruned_edge_does_not_promote(self) -> None:
        el = _build(_chain_spec(["a", "b"]), "b")
        # Deactivate the a -> b edge (as branch selection would).
        el.state.pruned_edges.add(("a", "b"))
        el.state.in_progress.add("a")
        el.complete_node_execution("a")
        assert "b" not in el.state.ready

    def test_skip_subtree_respects_shared_dependency(self) -> None:
        # c depends on both a and b; pruning a's branch must not skip c while
        # b (another upstream) still links to it.
        spec = {
            "a": {"inputs": {}},
            "b": {"inputs": {}},
            "c": {"inputs": {"x": ["a", 0], "y": ["b", 0]}},
        }
        el = _build(spec, "c")
        el._skip_subtree("a")
        # a has a dependent (c) so it is not skipped; c stays alive via b.
        assert "a" not in el.state.skipped
        assert "c" not in el.state.skipped


class TestReverseIndexReopen:
    def test_reopen_cascades_to_completed_dependents(self) -> None:
        el = _build(_chain_spec(["a", "b", "c"]), "c")
        # Run the whole chain to completion.
        for n in ("a", "b", "c"):
            el.state.in_progress.add(n)
            el.complete_node_execution(n)
        assert el.state.completed == {"a", "b", "c"}

        # Reopen the source: completed descendants (b, c) must be reset.
        el.reopen("a")
        assert "b" not in el.state.completed
        assert "c" not in el.state.completed
        assert "a" in el.state.ready


# ---------------------------------------------------------------------------
# Scaling benchmark: O(N^2) -> O(E)
# ---------------------------------------------------------------------------


def _complete_all(el: ExecutionList, order: list[str]) -> None:
    for n in order:
        el.state.in_progress.add(n)
        el.complete_node_execution(n)


def test_chain_scheduling_scales_linearly() -> None:
    """Executing an N-node chain must stay near-linear.

    The pre-index scheduler scanned *every* strong-link entry on each node
    completion to discover dependents (O(N) per completion -> O(N^2) over a
    chain of N completions). With the reverse dependents index a completion
    inspects only the node's single direct dependent, so executing the whole
    chain is O(N). We assert the 8x longer chain costs far less than the ~64x
    an O(N^2) law would imply.
    """
    def run(n: int) -> float:
        ids = [f"n{i}" for i in range(n)]
        spec = _chain_spec(ids)
        order = list(ids)  # source -> sink completion order
        start = time.perf_counter()
        for _ in range(20):
            el = _build(spec, ids[-1])
            _complete_all(el, order)
        return (time.perf_counter() - start) / 20

    small = run(200)
    large = run(1600)
    ratio = large / small if small > 0 else float("inf")
    print(f"\n[perf] chain scheduling — N=200: {small*1e3:.3f} ms, "
          f"N=1600: {large*1e3:.3f} ms, ratio: {ratio:.1f}x (quadratic would be ~64x)")
    # Linear expectation: 8x more nodes ~ 8x more work. Allow a generous ceiling
    # that an O(N^2) regression (~64x) would blow through.
    assert ratio < 24, f"scheduling regressed toward quadratic (ratio={ratio:.1f}x)"
