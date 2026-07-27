"""Contract tests for the API route manifest.

These guard three delivery-confidence invariants:
  * every critical ingress route is actually mounted,
  * OpenAPI operation IDs are unique (no silently-shadowed operations), and
  * a *required* router that fails to import raises at mount time instead of
    being logged-and-skipped.
"""

from __future__ import annotations

from collections import Counter

import pytest

from leagent.main import app


def test_critical_routes_are_present() -> None:
    paths = app.openapi()["paths"]
    expected = {
        "/api/v1/auth/login",
        "/api/v1/chat/stream",
        "/api/v1/chat/sessions",
        "/api/v1/files/upload",
        "/api/v1/workflow/flows/{flow_id}/run",
        "/api/v1/health/ready",
    }
    assert expected <= set(paths)


def test_openapi_operation_ids_are_unique() -> None:
    operation_ids = [
        operation["operationId"]
        for path_item in app.openapi()["paths"].values()
        for method, operation in path_item.items()
        if method.lower() in {"get", "post", "put", "patch", "delete", "head", "options"}
    ]
    duplicates = {
        key: count for key, count in Counter(operation_ids).items() if count > 1
    }
    assert duplicates == {}


def test_required_router_import_failure_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A required router that fails to import must raise, not warn-and-continue."""
    import importlib

    from fastapi import APIRouter

    from leagent.api.manifest import RouterSpec, mount_router

    real_import = importlib.import_module

    def _boom(name: str, *args: object, **kwargs: object) -> object:
        if name == "leagent.api.v1.chat":
            raise ImportError("simulated broken chat router")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(importlib, "import_module", _boom)

    spec = RouterSpec(import_path="leagent.api.v1.chat", prefix="/chat", required=True)
    with pytest.raises(RuntimeError, match="required API router failed to load"):
        mount_router(APIRouter(), spec)


def test_optional_router_import_failure_is_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An optional router import failure is logged and skipped, not raised."""
    import importlib

    from fastapi import APIRouter

    from leagent.api.manifest import RouterSpec, mount_router

    def _boom(name: str, *args: object, **kwargs: object) -> object:
        raise ImportError("simulated broken optional router")

    monkeypatch.setattr(importlib, "import_module", _boom)

    spec = RouterSpec(import_path="leagent.api.v1.nonexistent", required=False)
    # Must not raise.
    mount_router(APIRouter(), spec)
