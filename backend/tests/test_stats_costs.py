"""Contract tests for the ``/stats/costs`` cost-dashboard endpoints.

Traces are seeded straight into the app database (the same trick as
``test_missions_api``: sync ``sqlite3`` on ``LEAGENT_HOME/leagent.db``) because
the aggregates run server-side over ``agent_traces``. The soft budget is a
``user_settings`` row managed exclusively through the REST endpoints.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from leagent.config.settings import get_settings
from leagent.services.auth.service import LOCAL_USER_ID

if TYPE_CHECKING:
    from fastapi.testclient import TestClient

#: Prefix shared by every trace this module inserts (for teardown filtering).
_TRACE_PREFIX = "cost-test-"


def _insert_traces(rows: list[dict]) -> None:
    db_path = get_settings().database._sqlite_file_path()
    now = datetime.utcnow()
    with sqlite3.connect(db_path) as conn:
        for row in rows:
            conn.execute(
                """
                INSERT INTO agent_traces (
                    id, created_at, updated_at, trace_id, user_id, scope,
                    agent_name, model, status, started_at, latency_ms,
                    input_tokens, output_tokens, cache_read_tokens,
                    cache_miss_tokens, total_cost_usd, tool_call_count,
                    llm_call_count, error
                ) VALUES (?, ?, ?, ?, ?, 'chat_turn', 'agent', ?, ?, ?, 0,
                          ?, ?, ?, 0, ?, 0, 1, ?)
                """,
                (
                    uuid.uuid4().hex,
                    now.strftime("%Y-%m-%d %H:%M:%S.%f"),
                    now.strftime("%Y-%m-%d %H:%M:%S.%f"),
                    _TRACE_PREFIX + uuid.uuid4().hex[:12],
                    str(LOCAL_USER_ID),
                    row.get("model", "test-model"),
                    "failed" if row.get("error") else "completed",
                    row["started_at"].strftime("%Y-%m-%d %H:%M:%S.%f"),
                    row.get("input_tokens", 0),
                    row.get("output_tokens", 0),
                    row.get("cache_read_tokens", 0),
                    row.get("cost_usd", 0.0),
                    row.get("error"),
                ),
            )


def _cleanup_traces() -> None:
    db_path = get_settings().database._sqlite_file_path()
    with sqlite3.connect(db_path) as conn:
        conn.execute("DELETE FROM agent_traces WHERE trace_id LIKE ?", (_TRACE_PREFIX + "%",))


@pytest.fixture()
def seeded_traces():
    """Three traces today (one errored) + one stale trace 40 days back."""
    now = datetime.utcnow()
    minute = timedelta(minutes=1)
    _insert_traces(
        [
            {"started_at": now - minute, "cost_usd": 1.0, "input_tokens": 100, "output_tokens": 50},
            {"started_at": now - 2 * minute, "cost_usd": 2.0, "cache_read_tokens": 30},
            {
                "started_at": now - 3 * minute,
                "cost_usd": 0.5,
                "error": "boom",
                "model": "test-model-b",
            },
            {"started_at": now - timedelta(days=40), "cost_usd": 99.0},
        ]
    )
    try:
        yield
    finally:
        _cleanup_traces()


@pytest.fixture()
def clear_budget(client: TestClient):
    """Ensure no soft budget leaks between tests."""
    yield
    client.put("/api/v1/stats/costs/budget", json={"monthly_budget_usd": 0})


def test_costs_daily_aggregation(
    client: TestClient,
    seeded_traces,
    clear_budget,
) -> None:
    resp = client.get("/api/v1/stats/costs", params={"days": 7})
    assert resp.status_code == 200
    body = resp.json()

    # 7 daily buckets ending today; the 40-day-old trace must be excluded.
    assert len(body["days"]) == 7
    today = body["days"][-1]
    assert today["runs"] == 3
    assert today["errors"] == 1
    assert today["cost_usd"] == pytest.approx(3.5)
    assert today["input_tokens"] == 100
    assert today["output_tokens"] == 50
    assert today["cache_read_tokens"] == 30

    assert body["totals"]["runs"] == 3
    assert body["totals"]["errors"] == 1
    assert body["totals"]["cost_usd"] == pytest.approx(3.5)

    # No budget configured yet → null budget, no exceed flag, MTD still tracked.
    assert body["budget"]["monthly_budget_usd"] is None
    assert body["budget"]["month_to_date_usd"] == pytest.approx(3.5)
    assert body["budget"]["exceeded"] is False


def test_costs_budget_lifecycle(
    client: TestClient,
    seeded_traces,
    clear_budget,
) -> None:
    # Set a budget well above month-to-date spend.
    resp = client.put("/api/v1/stats/costs/budget", json={"monthly_budget_usd": 10.0})
    assert resp.status_code == 200
    assert resp.json() == {"monthly_budget_usd": 10.0}

    resp = client.get("/api/v1/stats/costs/budget")
    assert resp.status_code == 200
    assert resp.json()["monthly_budget_usd"] == 10.0

    body = client.get("/api/v1/stats/costs", params={"days": 7}).json()
    assert body["budget"]["monthly_budget_usd"] == 10.0
    assert body["budget"]["month_to_date_usd"] == pytest.approx(3.5)
    assert body["budget"]["pct"] == pytest.approx(35.0)
    assert body["budget"]["exceeded"] is False

    # Tighten below the spend → soft-exceeded flips on.
    client.put("/api/v1/stats/costs/budget", json={"monthly_budget_usd": 2.5})
    body = client.get("/api/v1/stats/costs", params={"days": 7}).json()
    assert body["budget"]["exceeded"] is True
    assert body["budget"]["pct"] == pytest.approx(140.0)
    # Zero clears the budget.
    resp = client.put("/api/v1/stats/costs/budget", json={"monthly_budget_usd": 0})
    assert resp.status_code == 200
    assert resp.json()["monthly_budget_usd"] is None
    body = client.get("/api/v1/stats/costs", params={"days": 7}).json()
    assert body["budget"]["monthly_budget_usd"] is None
    assert body["budget"]["exceeded"] is False


def test_put_budget_rejects_negative(client: TestClient, clear_budget) -> None:
    resp = client.put("/api/v1/stats/costs/budget", json={"monthly_budget_usd": -1.0})
    assert resp.status_code == 422


def test_costs_by_model(client: TestClient, seeded_traces) -> None:
    resp = client.get("/api/v1/stats/costs/by-model", params={"days": 30})
    assert resp.status_code == 200
    rows = {row["model"]: row for row in resp.json()}

    assert rows["test-model"]["runs"] == 2
    assert rows["test-model"]["cost_usd"] == pytest.approx(3.0)
    assert rows["test-model"]["total_tokens"] == 150
    assert rows["test-model"]["success_rate"] == 100.0

    assert rows["test-model-b"]["runs"] == 1
    assert rows["test-model-b"]["cost_usd"] == pytest.approx(0.5)
    assert rows["test-model-b"]["success_rate"] == 0.0

    # The 40-day-old trace belongs to no model bucket in a 30-day window.
    assert all(row["runs"] < 4 for row in rows.values())
