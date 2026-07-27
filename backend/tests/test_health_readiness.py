"""Readiness probe contract tests."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from leagent.api.deps import get_service_manager
from leagent.api.v1.health import router


class _ServiceManagerStub:
    def __init__(self, *, started: bool, database_ok: bool = True) -> None:
        self.is_started = started
        self.db: _DatabaseStub | None = _DatabaseStub(database_ok)
        self.cache: object | None = object()


class _DatabaseStub:
    def __init__(self, healthy: bool) -> None:
        self.healthy = healthy

    async def health_check(self) -> bool:
        return self.healthy


def _client_for(sm: _ServiceManagerStub) -> TestClient:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1/health")
    app.dependency_overrides[get_service_manager] = lambda: sm
    return TestClient(app)


def test_readiness_returns_503_when_services_are_not_started() -> None:
    client = _client_for(_ServiceManagerStub(started=False))

    response = client.get("/api/v1/health/ready")

    assert response.status_code == 503
    assert response.json() == {
        "ready": False,
        "checks": {
            "database": True,
            "cache": True,
            "services_started": False,
            "migrations": True,
        },
    }


def test_readiness_returns_200_when_services_are_started() -> None:
    client = _client_for(_ServiceManagerStub(started=True))

    response = client.get("/api/v1/health/ready")

    assert response.status_code == 200
    data: dict[str, Any] = response.json()
    assert data["ready"] is True
    assert data["checks"]["services_started"] is True
    assert data["checks"]["database"] is True
    assert data["checks"]["migrations"] is True


def test_readiness_returns_503_when_database_health_fails() -> None:
    client = _client_for(_ServiceManagerStub(started=True, database_ok=False))

    response = client.get("/api/v1/health/ready")

    assert response.status_code == 503
    assert response.json() == {
        "ready": False,
        "checks": {
            "database": False,
            "cache": True,
            "services_started": True,
            "migrations": False,
        },
    }
