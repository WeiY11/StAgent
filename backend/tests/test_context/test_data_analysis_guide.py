"""Gating tests for the data-analysis conversation-mode source."""

from __future__ import annotations

from leagent.context.recipe import RECIPE_REGISTRY
from leagent.context.sources import SOURCE_REGISTRY
from leagent.context.sources.gated_policy import DataAnalysisGuideSource

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
    from leagent.prompts.registry import get_prompt_registry

    policy = get_prompt_registry().get("policies/data_analysis_guide", variant="default")
    assert "Data analysis mode" in policy.body
    assert "data_profile" in policy.requires_tools
