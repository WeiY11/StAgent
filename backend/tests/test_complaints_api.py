"""Contract tests for the /api/v1/complaints endpoints.

Uses the shared session-scoped ``client`` fixture (real SQLite DB created via
``SQLModel.metadata.create_all``; auth disabled → LOCAL_USER_ID ownership).
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from fastapi.testclient import TestClient

BASE = "/api/v1/complaints"


def _create(client: TestClient, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "title": "Damaged package on arrival",
        "description": "Customer received a crushed box.",
        "customer_name": "Alice",
        "customer_contact": "alice@example.com",
        "category": "logistics",
        "severity": "high",
    }
    payload.update(overrides)
    resp = client.post(BASE, json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


class TestComplaintCrud:
    def test_create_returns_defaults(self, client: TestClient) -> None:
        body = _create(client)
        assert body["title"] == "Damaged package on arrival"
        assert body["category"] == "logistics"
        assert body["severity"] == "high"
        assert body["status"] == "open"
        assert body["source"] == "manual"
        assert body["resolved_at"] is None
        assert body["id"]

    def test_create_rejects_invalid_category(self, client: TestClient) -> None:
        resp = client.post(BASE, json={"title": "x", "category": "nonsense"})
        assert resp.status_code == 422

    def test_list_and_filters(self, client: TestClient) -> None:
        created = _create(client, title="Refund not processed", category="billing")
        _create(client, title="Rude support call", category="service", severity="low")

        resp = client.get(BASE)
        assert resp.status_code == 200, resp.text
        ids = {c["id"] for c in resp.json()}
        assert created["id"] in ids

        resp = client.get(BASE, params={"category": "billing"})
        assert resp.status_code == 200
        assert all(c["category"] == "billing" for c in resp.json())
        assert created["id"] in {c["id"] for c in resp.json()}

        resp = client.get(BASE, params={"status": "closed"})
        assert resp.status_code == 200
        assert created["id"] not in {c["id"] for c in resp.json()}

    def test_get_detail_and_missing(self, client: TestClient) -> None:
        created = _create(client)
        resp = client.get(f"{BASE}/{created['id']}")
        assert resp.status_code == 200, resp.text
        assert resp.json()["id"] == created["id"]

        resp = client.get(f"{BASE}/{uuid4()}")
        assert resp.status_code == 404

    def test_status_transition_resolved_sets_resolved_at(
        self, client: TestClient
    ) -> None:
        created = _create(client)

        resp = client.patch(
            f"{BASE}/{created['id']}", json={"status": "in_progress"}
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "in_progress"
        assert resp.json()["resolved_at"] is None

        resp = client.patch(
            f"{BASE}/{created['id']}",
            json={"status": "resolved", "resolution": "Reshipped the order."},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "resolved"
        assert body["resolution"] == "Reshipped the order."
        assert body["resolved_at"] is not None

    def test_update_missing_returns_404(self, client: TestClient) -> None:
        resp = client.patch(f"{BASE}/{uuid4()}", json={"status": "closed"})
        assert resp.status_code == 404

    def test_soft_delete_hides_from_list_and_detail(
        self, client: TestClient
    ) -> None:
        created = _create(client, title="To be deleted")

        resp = client.delete(f"{BASE}/{created['id']}")
        assert resp.status_code == 204, resp.text

        resp = client.get(f"{BASE}/{created['id']}")
        assert resp.status_code == 404

        resp = client.get(BASE)
        assert created["id"] not in {c["id"] for c in resp.json()}

        # Deleting again is a 404 (already soft-deleted).
        resp = client.delete(f"{BASE}/{created['id']}")
        assert resp.status_code == 404

    def test_stats_counts_by_status(self, client: TestClient) -> None:
        created = _create(client, title="Stats ticket")
        client.patch(f"{BASE}/{created['id']}", json={"status": "resolved"})

        resp = client.get(f"{BASE}/stats")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert set(body["by_status"]) == {"open", "in_progress", "resolved", "closed"}
        assert body["by_status"]["resolved"] >= 1
        assert body["total"] == sum(body["by_status"].values())
