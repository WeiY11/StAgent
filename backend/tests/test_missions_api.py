"""Mission REST API contract tests — fake planner/executor injected.

Covers create → planned → steps, terminal polling, list filtering, the
approve-step 409 contract, delete, and cross-user isolation (404). The
MissionService dependency is overridden with one whose planner/executor
factories are deterministic fakes, so no LLM or kernel runtime is needed.
"""

from __future__ import annotations

import time
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from StAgent.db.models.mission import Mission
from StAgent.mission.runner import AlwaysPassVerifier, StaticPlanner
from StAgent.mission.service import MissionService, get_mission_service
from StAgent.mission.types import MissionPlan, MissionStepState, SegmentResult

_TERMINAL = {"completed", "failed", "aborted", "expired"}


class FakeExecutor:
    """Instant successful segments; records the executed step numbers."""

    def __init__(self) -> None:
        self.calls: list[int] = []

    async def execute(self, *, mission: Mission, step: Any, prompt: str, attempt: int) -> SegmentResult:
        self.calls.append(int(step.step_no))
        return SegmentResult(
            reason="completed",
            text=f"step {step.step_no} done",
            usage={"total_tokens": 10, "cost_usd": 0.01},
        )


def _fake_plan() -> MissionPlan:
    return MissionPlan(
        goal="test goal",
        expected_output="done",
        steps=[
            MissionStepState(step_no=1, description="first step", kind="generic"),
            MissionStepState(step_no=2, description="second step", kind="generic", depends_on=[1]),
        ],
    )


@pytest.fixture()
def fake_executor() -> FakeExecutor:
    return FakeExecutor()


@pytest.fixture()
def mission_svc(app, fake_executor: FakeExecutor) -> MissionService:  # type: ignore[no-untyped-def]
    from StAgent.db import get_database_service

    service = MissionService(
        db=get_database_service(),
        planner_factory=lambda mission: StaticPlanner(_fake_plan()),
        executor_factory=lambda mission: fake_executor,
        verifier_factory=lambda mission: AlwaysPassVerifier(),
    )
    app.dependency_overrides[get_mission_service] = lambda: service
    return service


def _poll_terminal(client: TestClient, mission_id: str, timeout_s: float = 15.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    data: dict[str, Any] = {}
    while time.monotonic() < deadline:
        resp = client.get(f"/api/v1/missions/{mission_id}")
        assert resp.status_code == 200
        data = resp.json()
        if data["status"] in _TERMINAL:
            return data
        time.sleep(0.05)
    raise AssertionError(f"mission did not reach a terminal status in time: {data.get('status')}")


def test_create_runs_mission_to_completion(
    client: TestClient,
    mission_svc: MissionService,
    fake_executor: FakeExecutor,
) -> None:
    resp = client.post(
        "/api/v1/missions",
        json={
            "goal": "write a haiku",
            "autonomy": "full",
            "verify_level": "none",
            "budget": {"max_steps": 5},
        },
    )
    assert resp.status_code == 201, resp.text
    created = resp.json()
    mission_id = created["id"]
    assert created["goal"] == "write a haiku"
    assert created["status"] in {"planning", "ready", "running"}
    assert created["budget"]["max_steps"] == 5

    done = _poll_terminal(client, mission_id)
    assert done["status"] == "completed", done
    steps = done["steps"]
    assert [s["step_no"] for s in steps] == [1, 2]
    assert all(s["status"] == "passed" for s in steps)
    assert steps[1]["depends_on"] == [1]
    # Two segments × {total_tokens: 10} accumulate onto the usage ledger.
    assert done["usage"]["tokens"] == 20
    assert done["usage"]["segments"] == 2
    assert fake_executor.calls == [1, 2]

    listed = client.get("/api/v1/missions", params={"status": "completed"}).json()
    assert any(m["id"] == mission_id for m in listed)
    planning_only = client.get("/api/v1/missions", params={"status": "planning"}).json()
    assert all(m["id"] != mission_id for m in planning_only)


def test_delete_mission_removes_it(
    client: TestClient,
    mission_svc: MissionService,
) -> None:
    created = client.post("/api/v1/missions", json={"goal": "to delete"}).json()
    mission_id = created["id"]
    _poll_terminal(client, mission_id)

    assert client.delete(f"/api/v1/missions/{mission_id}").status_code == 204
    assert client.get(f"/api/v1/missions/{mission_id}").status_code == 404


def test_approve_step_conflicts_when_not_awaiting(
    client: TestClient,
    mission_svc: MissionService,
) -> None:
    created = client.post("/api/v1/missions", json={"goal": "no approvals"}).json()
    mission_id = created["id"]
    _poll_terminal(client, mission_id)

    resp = client.post(
        f"/api/v1/missions/{mission_id}/steps/1/approve",
        json={"approved": True, "note": ""},
    )
    assert resp.status_code == 409
    assert "awaiting approval" in resp.text


def test_unknown_mission_is_404(client: TestClient, mission_svc: MissionService) -> None:
    missing = str(uuid4())
    assert client.get(f"/api/v1/missions/{missing}").status_code == 404
    assert client.post(f"/api/v1/missions/{missing}/pause").status_code == 404
    assert client.delete(f"/api/v1/missions/{missing}").status_code == 404


def test_foreign_user_mission_is_404(client: TestClient, mission_svc: MissionService, app) -> None:  # type: ignore[no-untyped-def]
    """A mission owned by another user is indistinguishable from a missing one."""

    import sqlite3

    from StAgent.config.settings import get_settings
    from StAgent.services.auth.deps import get_current_user_id

    foreign_user = uuid4()
    # The mission FK targets ``users.id`` — materialize the foreign user row
    # first (sync sqlite3; the app DB is a plain SQLite file under StAgent_HOME).
    db_path = get_settings().database._sqlite_file_path()
    with sqlite3.connect(db_path) as conn:
        # ``users.id`` is CHAR(32): UUID hex without dashes.
        conn.execute(
            "INSERT OR IGNORE INTO users (id, username, role, disabled) VALUES (?, ?, ?, 0)",
            (foreign_user.hex, f"foreign-{foreign_user.hex[:8]}", "user"),
        )

    app.dependency_overrides[get_current_user_id] = lambda: foreign_user
    foreign_resp = client.post("/api/v1/missions", json={"goal": "another user's mission"})
    assert foreign_resp.status_code == 201, foreign_resp.text
    created = foreign_resp.json()
    foreign_id = created["id"]
    app.dependency_overrides.pop(get_current_user_id, None)

    assert client.get(f"/api/v1/missions/{foreign_id}").status_code == 404
    assert client.post(f"/api/v1/missions/{foreign_id}/pause").status_code == 404
    assert client.post(f"/api/v1/missions/{foreign_id}/resume").status_code == 404
    assert client.delete(f"/api/v1/missions/{foreign_id}").status_code == 404

    # Cleanup: back as the foreign owner, the mission is reachable and deletable.
    app.dependency_overrides[get_current_user_id] = lambda: foreign_user
    assert client.delete(f"/api/v1/missions/{foreign_id}").status_code == 204
    app.dependency_overrides.pop(get_current_user_id, None)


def test_create_validates_payload(client: TestClient, mission_svc: MissionService) -> None:
    assert client.post("/api/v1/missions", json={"goal": ""}).status_code == 422
    assert (
        client.post("/api/v1/missions", json={"goal": "x", "autonomy": "bogus"}).status_code == 422
    )
