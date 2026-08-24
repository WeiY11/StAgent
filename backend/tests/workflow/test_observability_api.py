"""HTTP surface tests for the observability router.

A minimal FastAPI app with auth and the workflow service stubbed, in the style of
``test_server_http``. These assert the read paths are reachable and correctly
shaped, that ownership is enforced, and that a chart survives the hub forgetting
about the run (the replay fallback).
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from StAgent.workflow.observability import ObservabilityHub
from StAgent.workflow.server.router import router as workflow_router

BASE = "/api/v1/workflow/observability"
ORIGIN = 1_700_000_000_000


def _iso(epoch_ms: int) -> str:
    """ISO form of an epoch stamp, so the row's clock agrees with its spans."""
    return datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc).isoformat()


USER_ID = uuid4()
OTHER_USER_ID = uuid4()
EXECUTION_ID = uuid4()


def _record(**overrides) -> dict:
    record = {
        "id": str(EXECUTION_ID),
        "prompt_id": "p-live",
        "flow_id": str(uuid4()),
        "flow_name": "Demo flow",
        "user_id": str(USER_ID),
        "status": "completed",
        "trigger_type": "manual",
        "node_count": 2,
        "duration_ms": 700,
        "error": None,
        "current_node": None,
        "started_at": _iso(ORIGIN),
        "completed_at": _iso(ORIGIN + 700),
        "created_at": _iso(ORIGIN),
        "cron_job_id": None,
        "cron_job_name": None,
        "execution_history": [
            {
                "node_id": "a",
                "status": "completed",
                "duration_ms": 400,
                "started_at_ms": ORIGIN,
                "finished_at_ms": ORIGIN + 400,
                "metadata": {},
            },
            {
                "node_id": "b",
                "status": "completed",
                "duration_ms": 300,
                "started_at_ms": ORIGIN + 400,
                "finished_at_ms": ORIGIN + 700,
                "metadata": {},
            },
        ],
    }
    record.update(overrides)
    return record


class _FakeWorkflowService:
    """Just enough of ``WorkflowService`` for the read endpoints."""

    def __init__(self, records: list[dict] | None = None) -> None:
        self.records = records if records is not None else [_record()]
        self.calls: list[dict] = []
        self.queue_positions: dict[str, int] = {}

    async def get_by_prompt_id(self, prompt_id: str) -> dict | None:
        return next(
            (r for r in self.records if r.get("prompt_id") == prompt_id), None
        )

    async def get_execution(self, execution_id) -> dict | None:
        return next(
            (r for r in self.records if str(r.get("id")) == str(execution_id)), None
        )

    async def list_all_executions(self, **kwargs):
        self.calls.append(kwargs)
        rows = list(self.records)
        offset = int(kwargs.get("offset") or 0)
        limit = int(kwargs.get("limit") or 20)
        return rows[offset : offset + limit], len(rows)

    async def queue_position(self, prompt_id: str):
        return self.queue_positions.get(prompt_id)


@pytest.fixture
def observability_app(monkeypatch: pytest.MonkeyPatch):
    """App + stubs. Yields ``(client, service, hub)``."""
    import StAgent.services.service_manager as sm_module
    import StAgent.workflow.server.observability_router as router_module
    from StAgent.services.auth.deps import get_current_user_id

    service = _FakeWorkflowService()
    hub = ObservabilityHub()

    class _FakeServiceManager:
        workflow_service = service

    monkeypatch.setattr(sm_module, "get_service_manager", lambda: _FakeServiceManager())
    monkeypatch.setattr(router_module, "get_observability_hub", lambda: hub)

    app = FastAPI()
    app.include_router(workflow_router, prefix="/api/v1")
    app.dependency_overrides[get_current_user_id] = lambda: USER_ID
    with TestClient(app) as client:
        yield client, service, hub
    app.dependency_overrides.clear()


def _feed_live_chart(hub: ObservabilityHub, prompt_id: str = "p-live") -> None:
    """Drive a two-node run through the hub and leave it running."""
    from StAgent.workflow.engine.progress import NodeProgressState, ProgressEvent

    def _state(node_id, started=None, finished=None):
        return NodeProgressState(
            node_id=node_id, started_at_ms=started, finished_at_ms=finished
        )

    hub.on_event(
        ProgressEvent(
            type="execution_start",
            prompt_id=prompt_id,
            data={
                "started_at_ms": ORIGIN,
                "node_total": 2,
                "flow_name": "Demo flow",
                "nodes": {"a": {"class_type": "T", "display_name": "Alpha"}},
            },
        )
    )
    hub.on_event(
        ProgressEvent(
            type="executing",
            prompt_id=prompt_id,
            node_id="a",
            state=_state("a", ORIGIN),
        )
    )
    hub.on_event(
        ProgressEvent(
            type="node_success",
            prompt_id=prompt_id,
            node_id="a",
            state=_state("a", ORIGIN, ORIGIN + 400),
        )
    )
    hub.on_event(
        ProgressEvent(
            type="executing",
            prompt_id=prompt_id,
            node_id="b",
            state=_state("b", ORIGIN + 400),
        )
    )


# ---------------------------------------------------------------------------
# Gantt
# ---------------------------------------------------------------------------


def test_gantt_prefers_the_live_chart(observability_app) -> None:
    client, _service, hub = observability_app
    _feed_live_chart(hub)

    resp = client.get(f"{BASE}/gantt/p-live")
    assert resp.status_code == 200
    body = resp.json()
    assert body["prompt_id"] == "p-live"
    assert body["approximate"] is False
    assert body["server_now_ms"] > 0
    bars = {b["node_id"]: b for b in body["bars"]}
    assert set(bars) == {"a", "b"}
    assert bars["a"]["display_name"] == "Alpha"
    assert bars["b"]["finished_at_ms"] is None, "the running bar stays open"
    # The DB row supplies the identity the event stream does not carry.
    assert body["execution_id"] == str(EXECUTION_ID)


def test_gantt_falls_back_to_replaying_the_persisted_row(observability_app) -> None:
    client, _service, _hub = observability_app  # hub is empty

    resp = client.get(f"{BASE}/gantt/p-live")
    assert resp.status_code == 200
    body = resp.json()
    assert body["approximate"] is False
    assert [b["node_id"] for b in body["bars"]] == ["a", "b"]
    assert body["lane_count"] == 1
    assert body["finished_at_ms"] is not None


def test_gantt_flags_a_legacy_row_as_approximate(observability_app) -> None:
    client, service, _hub = observability_app
    service.records = [
        _record(
            execution_history=[
                {"node_id": "a", "status": "completed", "duration_ms": 400},
                {"node_id": "b", "status": "completed", "duration_ms": 300},
            ]
        )
    ]

    body = client.get(f"{BASE}/gantt/p-live").json()
    assert body["approximate"] is True, "the UI must label this an approximate timeline"


def test_gantt_404s_for_an_unknown_prompt(observability_app) -> None:
    client, _service, _hub = observability_app
    assert client.get(f"{BASE}/gantt/nope").status_code == 404


def test_gantt_403s_for_another_users_execution(observability_app) -> None:
    client, service, hub = observability_app
    service.records = [_record(user_id=str(OTHER_USER_ID))]
    _feed_live_chart(hub)
    assert client.get(f"{BASE}/gantt/p-live").status_code == 403


def test_gantt_by_execution_is_not_shadowed_by_the_prompt_route(
    observability_app,
) -> None:
    client, _service, _hub = observability_app
    resp = client.get(f"{BASE}/gantt/by-execution/{EXECUTION_ID}")
    assert resp.status_code == 200
    assert resp.json()["execution_id"] == str(EXECUTION_ID)


def test_gantt_by_execution_404s_and_403s(observability_app) -> None:
    client, service, _hub = observability_app
    assert client.get(f"{BASE}/gantt/by-execution/{uuid4()}").status_code == 404
    service.records = [_record(user_id=str(OTHER_USER_ID))]
    assert (
        client.get(f"{BASE}/gantt/by-execution/{EXECUTION_ID}").status_code == 403
    )


# ---------------------------------------------------------------------------
# Live wall
# ---------------------------------------------------------------------------


def test_live_lists_only_unfinished_executions(observability_app) -> None:
    client, service, hub = observability_app
    service.records = [
        _record(prompt_id="p-live", status="running"),
        _record(prompt_id="p-done", status="completed", id=str(uuid4())),
    ]
    _feed_live_chart(hub)

    body = client.get(f"{BASE}/live").json()
    assert body["scanned"] == 2
    assert body["total"] == 1
    (item,) = body["items"]
    assert item["prompt_id"] == "p-live"
    assert item["flow_name"] == "Demo flow"
    assert item["current_nodes"] == ["b"]
    assert item["node_done"] == 1
    assert item["node_total"] == 2
    assert item["queue_position"] is None
    # A card-sized chart rides along so the wall can draw a sparkline.
    assert item["gantt"]["prompt_id"] == "p-live"


def test_live_reports_a_queue_position_when_a_broker_offers_one(
    observability_app,
) -> None:
    client, service, hub = observability_app
    service.records = [_record(prompt_id="p-live", status="queued")]
    service.queue_positions["p-live"] = 3
    _feed_live_chart(hub)

    (item,) = client.get(f"{BASE}/live").json()["items"]
    assert item["queue_position"] == 3


def test_live_survives_a_broker_without_queue_support(observability_app) -> None:
    client, service, _hub = observability_app
    service.records = [_record(prompt_id="p-live", status="running")]

    async def _boom(prompt_id):  # noqa: ANN001, ANN202
        raise RuntimeError("no queue configured")

    service.queue_position = _boom
    (item,) = client.get(f"{BASE}/live").json()["items"]
    assert item["queue_position"] is None
    # No hub chart, so progress falls back to the DB row's current node.
    assert item["gantt"] is None


def test_live_is_empty_when_nothing_is_running(observability_app) -> None:
    client, _service, _hub = observability_app  # the default record is completed
    body = client.get(f"{BASE}/live").json()
    assert body["items"] == []
    assert body["total"] == 0


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------


def test_profile_reports_the_bottleneck(observability_app) -> None:
    client, _service, _hub = observability_app

    resp = client.get(f"{BASE}/profile/p-live")
    assert resp.status_code == 200
    body = resp.json()
    assert body["prompt_id"] == "p-live"
    assert body["bottleneck"]["node_id"] == "a"  # 400ms beats 300ms
    assert body["total_ms"] == 700
    assert body["critical_path_ms"] == 700
    assert body["parallelism"] == 1.0
    assert body["waiting_human_ms"] == 0
    assert body["approximate"] is False
    assert [s["node_id"] for s in body["shares"]] == ["a", "b"]


def test_profile_404s_for_an_unknown_prompt(observability_app) -> None:
    client, _service, _hub = observability_app
    assert client.get(f"{BASE}/profile/nope").status_code == 404


def test_profile_403s_for_another_users_execution(observability_app) -> None:
    client, service, _hub = observability_app
    service.records = [_record(user_id=str(OTHER_USER_ID))]
    assert client.get(f"{BASE}/profile/p-live").status_code == 403


# ---------------------------------------------------------------------------
# Global execution history
# ---------------------------------------------------------------------------


def test_executions_returns_a_trimmed_row(observability_app) -> None:
    client, _service, _hub = observability_app

    resp = client.get(f"{BASE}/executions")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["limit"] == 20
    assert body["offset"] == 0
    (row,) = body["items"]
    assert row["prompt_id"] == "p-live"
    assert row["flow_name"] == "Demo flow"
    assert row["duration_ms"] == 700
    # A page of 200 rows must not drag every node payload across the wire.
    assert "execution_history" not in row
    assert "inputs" not in row
    assert "outputs" not in row


def test_executions_forwards_every_filter(observability_app) -> None:
    client, service, _hub = observability_app
    flow_id = str(uuid4())

    resp = client.get(
        f"{BASE}/executions",
        params={
            "status": "failed",
            "flow_id": flow_id,
            "trigger_type": "cron",
            "q": "demo",
            "started_after": "2026-01-01T00:00:00Z",
            "started_before": "2026-02-01T00:00:00+00:00",
            "limit": 5,
            "offset": 10,
        },
    )
    assert resp.status_code == 200
    (call,) = service.calls
    assert call["status"] == "failed"
    assert str(call["flow_id"]) == flow_id
    assert call["trigger_type"] == "cron"
    assert call["q"] == "demo"
    assert call["limit"] == 5
    assert call["offset"] == 10
    assert call["user_id"] == USER_ID
    assert call["started_after"].year == 2026
    assert call["started_before"].month == 2


def test_executions_rejects_a_malformed_timestamp(observability_app) -> None:
    client, _service, _hub = observability_app
    resp = client.get(f"{BASE}/executions", params={"started_after": "yesterday"})
    assert resp.status_code == 422


def test_executions_clamps_the_page_size(observability_app) -> None:
    client, _service, _hub = observability_app
    assert client.get(f"{BASE}/executions", params={"limit": 5000}).status_code == 422
    assert client.get(f"{BASE}/executions", params={"offset": -1}).status_code == 422


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_every_documented_route_is_registered() -> None:
    paths = {
        getattr(route, "path", "")
        for route in workflow_router.routes
        if "observability" in getattr(route, "path", "")
    }
    assert paths == {
        "/workflow/observability/gantt/{prompt_id}",
        "/workflow/observability/gantt/by-execution/{execution_id}",
        "/workflow/observability/live",
        "/workflow/observability/profile/{prompt_id}",
        "/workflow/observability/executions",
        "/workflow/observability/stream/{prompt_id}",
    }
