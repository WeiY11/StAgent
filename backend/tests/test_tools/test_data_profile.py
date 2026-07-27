"""Unit tests for the data_profile tool."""

from __future__ import annotations

from leagent.tools.base import ToolContext
from leagent.tools.data.data_profile import DataProfileTool


def make_ctx() -> ToolContext:
    return ToolContext(user_id="u1", session_id="s1")


SALES = [
    {"date": "2026-01-01", "region": "North", "revenue": 120.0, "units": 10},
    {"date": "2026-01-02", "region": "South", "revenue": 180.0, "units": 15},
    {"date": "2026-01-03", "region": "North", "revenue": 150.0, "units": 12},
    {"date": "2026-01-04", "region": "South", "revenue": 220.0, "units": 18},
    {"date": "2026-01-05", "region": "North", "revenue": None, "units": 11},
]


def test_profile_basic_shapes_and_roles():
    result = DataProfileTool().execute_sync({"data": SALES}, make_ctx())

    assert result["overview"]["rows"] == 5
    assert result["overview"]["columns"] == 4
    by_name = {c["name"]: c for c in result["columns"]}
    assert by_name["revenue"]["role"] == "numeric"
    assert by_name["units"]["role"] == "numeric"
    assert by_name["date"]["role"] == "datetime"
    assert by_name["region"]["role"] == "categorical"
    assert by_name["revenue"]["null_count"] == 1


def test_profile_numeric_stats_and_correlations():
    result = DataProfileTool().execute_sync({"data": SALES}, make_ctx())

    stats = {c["name"]: c for c in result["columns"]}["units"]["stats"]
    assert stats["min"] == 10.0
    assert stats["max"] == 18.0
    # revenue/units strongly correlated in this fixture
    assert result["correlations"]
    assert set(result["correlations"][0]["columns"]) == {"revenue", "units"}


def test_profile_quality_warnings():
    rows = [
        {"id": i, "flag": "same", "score": 50 if i % 2 else None}
        for i in range(30)
    ]
    rows.append(dict(rows[0]))  # duplicate row
    result = DataProfileTool().execute_sync({"data": rows}, make_ctx())

    types = {w["type"] for w in result["warnings"]}
    assert "duplicate_rows" in types
    assert "constant_column" in types
    assert "high_null_ratio" in types


def test_profile_suggestions_follow_roles():
    result = DataProfileTool().execute_sync({"data": SALES}, make_ctx())

    joined = " ".join(result["suggested_analyses"]).lower()
    assert "trend" in joined
    assert "group" in joined
    assert 1 <= len(result["suggested_analyses"]) <= 5


def test_profile_empty_dataset():
    result = DataProfileTool().execute_sync({"data": []}, make_ctx())

    assert result["overview"]["rows"] == 0
    assert result["warnings"][0]["type"] == "empty_dataset"


def test_profile_column_filter_and_missing_columns():
    result = DataProfileTool().execute_sync(
        {"data": SALES, "columns": ["revenue", "nope"]}, make_ctx()
    )

    assert [c["name"] for c in result["columns"]] == ["revenue"]
    assert any(w["type"] == "missing_columns" for w in result["warnings"])
