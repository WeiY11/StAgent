"""Gating tests for the data-analysis conversation-mode source."""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest

from StAgent.context.recipe import RECIPE_REGISTRY
from StAgent.context.sources import SOURCE_REGISTRY
from StAgent.context.sources.base import ResolveContext
from StAgent.context.sources.gated_policy import DataAnalysisGuideSource

GATE = DataAnalysisGuideSource.gate


def test_registered_in_source_registry_and_recipes():
    assert SOURCE_REGISTRY["data_analysis_guide"] is DataAnalysisGuideSource
    for recipe in ("default_agent", "script_agent", "coding_agent"):
        ids = [e.source_id for e in RECIPE_REGISTRY[recipe].entries]
        assert "data_analysis_guide" in ids


def test_gate_opens_on_analysis_queries():
    assert GATE.matches("帮我分析一下这个 CSV")
    assert GATE.matches("做一次数据分析，看看销售趋势")
    assert GATE.matches("Run an EDA on this dataset")
    assert GATE.matches("check the data quality of this spreadsheet")


def test_gate_stays_closed_on_unrelated_queries():
    assert not GATE.matches("今天天气怎么样")
    assert not GATE.matches("write a poem about the sea")
    assert not GATE.matches(None)


def test_gate_opt_in_keys():
    assert GATE.matches("hello", template_vars={"enable_data_analysis": True})
    assert GATE.matches("hello", template_vars={"data_analysis_guide": 1})
    assert not GATE.matches("hello", template_vars={"enable_data_analysis": False})


def test_policy_template_exists_in_registry():
    from StAgent.prompts.registry import get_prompt_registry

    policy = get_prompt_registry().get("policies/data_analysis_guide", variant="default")
    assert "Data analysis mode" in policy.body
    assert "data_profile" in policy.requires_tools


# -- tabular-attachment opt-in + snapshot summary injection -----------------


class _Attachment:
    """Minimal stand-in for SessionAttachment (only fields the source reads)."""

    def __init__(self, filename: str, extra: dict[str, Any] | None = None) -> None:
        self.filename = filename
        self.extra = extra or {}


class _FakeSessionManager:
    def __init__(self, attachments: list[_Attachment]) -> None:
        self._attachments = attachments

    async def list_attachments(self, session_id: UUID, *, user_id: UUID | None = None) -> list[_Attachment]:
        return self._attachments


_CSV_SNAPSHOT: dict[str, Any] = {
    "columns": [
        {"name": "region", "dtype": "object", "null_pct": 0.0},
        {"name": "revenue", "dtype": "float64", "null_pct": 7.0},
    ],
    "row_count_estimate": 1234,
    "sample_rows": [{"region": "east", "revenue": 10.5}],
    "truncated": True,
}


def _ctx(attachments: list[_Attachment] | None, query: str) -> ResolveContext:
    from StAgent.prompts.registry import get_prompt_registry

    sm = _FakeSessionManager(attachments) if attachments is not None else None
    return ResolveContext(
        query=query,
        session_manager=sm,
        session_id=uuid4() if sm is not None else None,
        prompt_registry=get_prompt_registry(),
    )


@pytest.mark.asyncio
async def test_tabular_attachment_forces_gate_open_without_keywords():
    ctx = _ctx([_Attachment("sales.csv", {"tabular_snapshot": _CSV_SNAPSHOT})], "hello")
    block = await DataAnalysisGuideSource().resolve(ctx)
    assert block is not None
    assert "Data analysis mode" in block.body
    assert block.metadata["tabular_snapshots"] == 1


@pytest.mark.asyncio
async def test_no_attachments_and_no_keywords_skips_source():
    ctx = _ctx([], "write a poem about the sea")
    assert await DataAnalysisGuideSource().resolve(ctx) is None
    # Also with no session manager wired at all.
    assert await DataAnalysisGuideSource().resolve(_ctx(None, "hello")) is None


@pytest.mark.asyncio
async def test_non_tabular_attachment_does_not_open_gate():
    ctx = _ctx([_Attachment("photo.png", {})], "hello")
    assert await DataAnalysisGuideSource().resolve(ctx) is None


@pytest.mark.asyncio
async def test_snapshot_summary_appears_in_resolved_block():
    ctx = _ctx([_Attachment("sales.csv", {"tabular_snapshot": _CSV_SNAPSHOT})], "分析一下")
    block = await DataAnalysisGuideSource().resolve(ctx)
    assert block is not None
    assert "### Attached tabular datasets" in block.body
    assert "sales.csv" in block.body
    assert "~1234 rows" in block.body
    assert "region(object)" in block.body
    assert "revenue(float64)" in block.body


@pytest.mark.asyncio
async def test_large_file_snapshot_summary_hints_chunked_processing():
    snapshot = {"file_size": 60 * 1024 * 1024, "truncated": True, "hint": "large_file"}
    ctx = _ctx([_Attachment("big.csv", {"tabular_snapshot": snapshot})], "hello")
    block = await DataAnalysisGuideSource().resolve(ctx)
    assert block is not None
    assert "big.csv" in block.body
    assert "large file" in block.body
    assert "code_execution" in block.body


def test_summary_line_capped_at_400_chars():
    from StAgent.context.sources.gated_policy import _summarise_tabular_snapshot

    wide = {
        "columns": [{"name": f"column_{i}_with_a_long_name", "dtype": "float64", "null_pct": 0.0} for i in range(60)],
        "row_count_estimate": 999_999,
        "sample_rows": [],
        "truncated": False,
    }
    line = _summarise_tabular_snapshot("wide.csv", wide)
    assert len(line) <= 400
    assert line.startswith("- wide.csv:")
