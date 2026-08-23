from __future__ import annotations

import json
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from StAgent.db import get_database_service
from StAgent.db.models import Flow
from StAgent.services.auth import deps as auth_deps
from StAgent.services.auth.deps import get_current_user_id
from StAgent.services.auth.deps import UserPrincipal
from StAgent.services.auth.service import AuthService, init_auth_service
from StAgent.workflow.server.flow_permissions import can_access_flow
from StAgent.workflow.server.router import router as workflow_router


def _principal(user_id: UUID, *, admin: bool = False) -> UserPrincipal:
    return UserPrincipal(
        user_id=user_id,
        roles=frozenset({"admin"} if admin else {"user"}),
        permissions=frozenset({"*"} if admin else ()),
        is_superuser=admin,
        username="admin" if admin else "user",
    )


def _flow(owner_id: UUID, *, is_public: bool = False, settings: dict | None = None) -> Flow:
    return Flow(
        name="flow",
        user_id=owner_id,
        is_public=is_public,
        settings=json.dumps(settings) if settings is not None else None,
    )


def test_owner_can_manage_private_flow() -> None:
    owner = uuid4()
    flow = _flow(owner)
    principal = _principal(owner)

    assert can_access_flow(flow, principal, "view")
    assert can_access_flow(flow, principal, "edit")
    assert can_access_flow(flow, principal, "run")
    assert can_access_flow(flow, principal, "delete")


def test_public_flow_defaults_to_view_and_run_only() -> None:
    owner = uuid4()
    other = _principal(uuid4())
    flow = _flow(owner, is_public=True)

    assert can_access_flow(flow, other, "view")
    assert can_access_flow(flow, other, "run")
    assert not can_access_flow(flow, other, "edit")
    assert not can_access_flow(flow, other, "delete")


def test_explicit_permissions_allow_view_and_run_without_edit() -> None:
    owner = uuid4()
    other = _principal(uuid4())
    flow = _flow(
        owner,
        settings={
            "permissions": {
                "canView": ["all"],
                "canRun": ["all"],
                "canEdit": ["owner"],
            },
        },
    )

    assert can_access_flow(flow, other, "view")
    assert can_access_flow(flow, other, "run")
    assert not can_access_flow(flow, other, "edit")
    assert not can_access_flow(flow, other, "delete")


def test_admin_permission_requires_admin_principal() -> None:
    owner = uuid4()
    normal = _principal(uuid4())
    admin = _principal(uuid4(), admin=True)
    flow = _flow(
        owner,
        settings={
            "permissions": {
                "canView": ["admin"],
                "canRun": ["admin"],
                "canEdit": ["admin"],
            },
        },
    )

    assert not can_access_flow(flow, normal, "view")
    assert not can_access_flow(flow, normal, "run")
    assert not can_access_flow(flow, normal, "edit")
    assert can_access_flow(flow, admin, "view")
    assert can_access_flow(flow, admin, "run")
    assert can_access_flow(flow, admin, "edit")
    assert can_access_flow(flow, admin, "delete")


def test_invalid_settings_fall_back_to_public_flag() -> None:
    owner = uuid4()
    other = _principal(uuid4())
    flow = _flow(owner, is_public=False)
    flow.settings = "{not-json"

    assert not can_access_flow(flow, other, "view")
    assert not can_access_flow(flow, other, "run")


def _client_for_flow(flow: Flow, user_id: UUID) -> TestClient:
    app = FastAPI()
    app.include_router(workflow_router, prefix="/api/v1")

    async def _fake_user() -> UUID:
        return user_id

    class _Session:
        async def __aenter__(self) -> "_Session":
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def get(self, _model: object, flow_id: UUID) -> Flow | None:
            return flow if str(flow_id) == str(flow.id) else None

        def add(self, _obj: object) -> None:
            return None

        async def flush(self) -> None:
            return None

        async def refresh(self, _obj: object) -> None:
            return None

    class _DB:
        def session(self) -> _Session:
            return _Session()

    app.dependency_overrides[get_current_user_id] = _fake_user
    app.dependency_overrides[get_database_service] = lambda: _DB()
    return TestClient(app)


def test_get_flow_denies_private_flow_to_non_owner() -> None:
    flow = _flow(uuid4())
    other = uuid4()
    client = _client_for_flow(flow, other)

    response = client.get(f"/api/v1/workflow/flows/{flow.id}")

    assert response.status_code == 403


def test_http_flow_permissions_apply_different_view_and_run_rules() -> None:
    flow = _flow(
        uuid4(),
        settings={
            "permissions": {
                "canView": ["all"],
                "canRun": ["owner"],
                "canEdit": ["owner"],
            },
        },
    )
    other = uuid4()
    client = _client_for_flow(flow, other)

    view = client.get(f"/api/v1/workflow/flows/{flow.id}")
    run = client.post(f"/api/v1/workflow/flows/{flow.id}/run", json={"input_data": {}})

    assert view.status_code == 200
    assert run.status_code == 403


def test_patch_flow_updates_settings_and_returns_settings() -> None:
    owner = uuid4()
    flow = _flow(owner)
    client = _client_for_flow(flow, owner)
    settings = {
        "permissions": {
            "canView": ["owner"],
            "canRun": ["owner"],
            "canEdit": ["owner"],
        },
    }

    response = client.patch(
        f"/api/v1/workflow/flows/{flow.id}",
        json={"settings": settings, "is_public": False},
    )

    assert response.status_code == 200
    assert json.loads(response.json()["settings"]) == settings
    assert json.loads(flow.settings or "{}") == settings


def test_get_validate_saved_flow_uses_stored_document(sample_canonical_document: dict) -> None:
    import asyncio

    from StAgent.workflow.nodes import bootstrap

    asyncio.run(bootstrap())
    owner = uuid4()
    flow = _flow(owner)
    flow.data = json.dumps(sample_canonical_document)
    client = _client_for_flow(flow, owner)

    response = client.get(f"/api/v1/workflow/flows/{flow.id}/validate")

    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_workflow_admin_http_routes_require_admin_permission(monkeypatch) -> None:
    class _Sec:
        enforce_auth = True

    class _Settings:
        host = "0.0.0.0"
        desktop_mode = False
        secret_key = "unit-test-secret-key-32bytes-min"
        security = _Sec()
        files = type("Files", (), {"signed_url_secret": ""})()

    app = FastAPI()
    app.include_router(workflow_router, prefix="/api/v1")
    svc = AuthService(_Settings())
    monkeypatch.setattr(auth_deps, "_auth_enforced", lambda: True)
    init_auth_service(_Settings())
    try:
        user_token = svc.create_access_token(uuid4(), role="user", username="user")
        admin_token = svc.create_access_token(uuid4(), role="admin", username="admin")
        client = TestClient(app)

        no_token = client.get("/api/v1/workflow/admin/replacements")
        user = client.get(
            "/api/v1/workflow/admin/replacements",
            headers={"Authorization": f"Bearer {user_token}"},
        )
        admin = client.get(
            "/api/v1/workflow/admin/replacements",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
    finally:
        init_auth_service(None)

    assert no_token.status_code == 401
    assert user.status_code == 403
    assert admin.status_code == 200
