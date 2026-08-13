"""Runtime boundary tests for the bounded quality-refinement loop."""

from __future__ import annotations

import pytest

from leagent.workflow.io import HiddenHolder
from leagent.workflow.nodes.builtin.iterative_refine import IterativeRefineNode
from leagent.workflow.nodes.builtin.quality_gate import QualityGateNode


@pytest.mark.asyncio
async def test_quality_gate_falls_back_for_invalid_numeric_inputs() -> None:
    output = await QualityGateNode().execute(
        hidden=HiddenHolder(unique_id="quality"),
        score="not-a-number",
        threshold="not-a-number",
        base_score="not-a-number",
        score_step="not-a-number",
    )

    assert output.values[:2] == (0.45, False)
    assert output.metadata["threshold"] == 0.7


@pytest.mark.asyncio
async def test_iterative_refine_falls_back_for_invalid_iteration_limit() -> None:
    output = await IterativeRefineNode().execute(
        hidden=HiddenHolder(unique_id="refine"),
        max_iterations="not-an-integer",
    )

    assert output.values == (1, False)
    assert output.metadata["max_iterations"] == 3
