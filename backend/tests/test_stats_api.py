"""Contract tests for the ``/stats/cache`` observability + config endpoints.

The runtime cache is process-local and non-durable; the config payload must say
so explicitly (``persistent: false``) so operators never assume a Redis-backed
cache exists behind the API.
"""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_cache_config_response_is_explicitly_ephemeral(
    client: TestClient,
) -> None:
    response = client.get("/api/v1/stats/cache")
    assert response.status_code == 200
    config = response.json()["config"]
    assert config is not None
    assert config["persistent"] is False
    assert config["backend"] == "memory"


def test_cache_config_rejects_max_items_below_shard_count(
    client: TestClient,
) -> None:
    # shard_count defaults to 1, so max_items=0 would drop below it. The mutation
    # endpoint must translate the ValueError into an HTTP 422 instead of 500.
    response = client.post("/api/v1/stats/cache/config", json={"max_items": 0})
    assert response.status_code == 422
