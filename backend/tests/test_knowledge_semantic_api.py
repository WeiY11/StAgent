"""Contract tests for the local semantic model lifecycle API."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

import StAgent.config.config as runtime_config
from StAgent.api.v1 import knowledge_semantic as semantic_api
from StAgent.config.settings import Settings
from StAgent.services.service_manager import ServiceManager


def test_semantic_router_exposes_all_public_operations() -> None:
    paths = {
        (route.path, tuple(sorted(route.methods or ())))
        for route in semantic_api.router.routes
        if isinstance(route, APIRoute)
    }
    assert ("/status", ("GET",)) in paths
    assert ("/models", ("GET",)) in paths
    assert ("/models/import", ("POST",)) in paths
    assert ("/models/download", ("POST",)) in paths
    assert ("/models/{model_id}", ("DELETE",)) in paths
    assert ("/config", ("PUT",)) in paths
    assert ("/rebuild", ("POST",)) in paths


def test_safe_model_response_omits_source_path_and_raw_prompts() -> None:
    record = SimpleNamespace(
        model_id="local-test",
        display_name="Test model",
        source_type="local",
        source_reference="C:/private/model",
        resolved_revision="a" * 40,
        dimension=384,
        query_prompt="query: ",
        document_prompt="passage: ",
        normalize=True,
        file_size=10,
        status="ready",
        fingerprint="b" * 64,
    )

    payload = semantic_api._safe_model(record)

    assert payload["model_id"] == "local-test"
    assert payload["query_prompt_configured"] is True
    assert payload["document_prompt_configured"] is True
    assert "source_reference" not in payload
    assert "source_path" not in payload
    assert payload["fingerprint"] == "b" * 64


@pytest.mark.asyncio
async def test_config_rejects_local_backend_for_multiple_workers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SimpleNamespace(
        settings=SimpleNamespace(workers=2),
        knowledge_semantic_status={"environment_managed": False},
    )
    monkeypatch.setattr(semantic_api, "get_service_manager", lambda: manager)

    with pytest.raises(HTTPException) as exc_info:
        await semantic_api.update_semantic_config(
            semantic_api.SemanticConfigBody(enabled=True, local_model_id="local-test")
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == "local_backend_requires_single_worker_use_milvus"


@pytest.mark.asyncio
async def test_config_rejects_unknown_model_before_persisting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SimpleNamespace(
        settings=SimpleNamespace(workers=1),
        knowledge_semantic_status={"environment_managed": False},
        db=None,
        task_manager=None,
    )
    config = SimpleNamespace(
        knowledge=SimpleNamespace(
            semantic=SimpleNamespace(
                enabled=False,
                vector_backend="sqlite-vec",
                local_model_id="",
                auto_reindex=True,
            )
        )
    )
    saved: list[object] = []

    monkeypatch.setattr(semantic_api, "get_service_manager", lambda: manager)
    monkeypatch.setattr(semantic_api, "load_config", lambda: config)
    monkeypatch.setattr(semantic_api, "save_config", lambda config_value: saved.append(config_value))
    monkeypatch.setattr(
        semantic_api,
        "_registry",
        lambda: SimpleNamespace(
            models_dir=Path("C:/managed/models"),
            get=lambda _model_id: None,
        ),
    )

    with pytest.raises(HTTPException) as exc_info:
        await semantic_api.update_semantic_config(
            semantic_api.SemanticConfigBody(enabled=True, local_model_id="missing")
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "local_model_not_ready"
    assert saved == []


@pytest.mark.asyncio
async def test_config_persists_valid_local_model_then_restarts_and_queues_rebuild(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A valid opt-in survives a fresh manager and immediately schedules its projection."""
    config_path = tmp_path / "config.yaml"
    models_dir = tmp_path / "knowledge" / "models"
    model_id = "local-valid"
    (models_dir / model_id).mkdir(parents=True)
    record = SimpleNamespace(model_id=model_id, status="ready", dimension=384)

    class _Registry:
        def __init__(self) -> None:
            self.models_dir = models_dir

        def get(self, requested_model_id: str) -> object | None:
            return record if requested_model_id == model_id else None

    class _Manager:
        def __init__(self) -> None:
            self.settings = SimpleNamespace(workers=1)
            self.db = None
            self.task_manager = None
            self.knowledge_semantic_status = {
                "environment_managed": False,
                "status": "lexical_only",
                "task_id": None,
            }
            self.retrieval_restarts = 0
            self.rebuilds_queued = 0

        async def _start_knowledge_retrieval(self) -> None:
            self.retrieval_restarts += 1
            self.knowledge_semantic_status["status"] = "building"

        async def enqueue_knowledge_vector_rebuild(self) -> str:
            self.rebuilds_queued += 1
            self.knowledge_semantic_status["task_id"] = "semantic-task"
            return "semantic-task"

    for name in (
        "StAgent_KNOWLEDGE__SEMANTIC__ENABLED",
        "StAgent_KNOWLEDGE_SEMANTIC_ENABLED",
        "StAgent_KNOWLEDGE__SEMANTIC__VECTOR_BACKEND",
        "StAgent_KNOWLEDGE_SEMANTIC_VECTOR_BACKEND",
        "StAgent_KNOWLEDGE__SEMANTIC__LOCAL_MODEL_ID",
        "StAgent_KNOWLEDGE_SEMANTIC_LOCAL_MODEL_ID",
        "StAgent_KNOWLEDGE__SEMANTIC__AUTO_REINDEX",
        "StAgent_KNOWLEDGE_SEMANTIC_AUTO_REINDEX",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(runtime_config, "CONFIG_PATH", config_path)
    manager = _Manager()
    monkeypatch.setattr(semantic_api, "get_service_manager", lambda: manager)
    monkeypatch.setattr(semantic_api, "_registry", _Registry)

    payload = await semantic_api.update_semantic_config(
        semantic_api.SemanticConfigBody(
            enabled=True,
            vector_backend="sqlite-vec",
            local_model_id=model_id,
            auto_reindex=True,
        )
    )

    persisted = runtime_config.load_config().knowledge.semantic
    restored = ServiceManager(Settings())._effective_knowledge_semantic_config()
    assert persisted.enabled is True
    assert persisted.vector_backend == "sqlite-vec"
    assert persisted.local_model_id == model_id
    assert persisted.auto_reindex is True
    assert restored.enabled is True
    assert restored.local_model_id == model_id
    assert restored.environment_managed is False
    assert manager.retrieval_restarts == 1
    assert manager.rebuilds_queued == 1
    assert payload == {
        "environment_managed": False,
        "status": "building",
        "task_id": "semantic-task",
    }


@pytest.mark.asyncio
async def test_delete_model_restores_directory_when_catalog_read_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    models_dir = tmp_path / "models"
    staging_dir = models_dir / ".staging"
    target = models_dir / "local-test"
    target.mkdir(parents=True)
    (target / "config.json").write_text("{}", encoding="utf-8")
    staging_dir.mkdir()
    record = SimpleNamespace(model_id="local-test", fingerprint="a" * 64)

    class _Registry:
        def __init__(self) -> None:
            self.models_dir = models_dir
            self.staging_dir = staging_dir

        def get(self, model_id: str) -> object | None:
            return record if model_id == record.model_id else None

        def _read_catalog(self) -> dict[str, object]:
            raise OSError("catalog unavailable")

    monkeypatch.setattr(
        semantic_api,
        "get_service_manager",
        lambda: SimpleNamespace(
            knowledge_semantic_status={
                "model_id": None,
                "active_version": None,
                "candidate_version": None,
            }
        ),
    )
    monkeypatch.setattr(semantic_api, "_registry", lambda: _Registry())

    with pytest.raises(OSError, match="catalog unavailable"):
        await semantic_api.delete_model("local-test")

    assert target.is_dir()
    assert (target / "config.json").read_text(encoding="utf-8") == "{}"
    assert not any(staging_dir.iterdir())


def test_admin_operations_carry_admin_panel_dependency() -> None:
    admin_routes = {
        "/models/import",
        "/models/download",
        "/models",
        "/models/{model_id}",
        "/config",
        "/rebuild",
    }
    for route in semantic_api.router.routes:
        if not isinstance(route, APIRoute) or route.path not in admin_routes:
            continue
        dependency_names = {
            type(dep.call).__name__ if not hasattr(dep.call, "__qualname__") else dep.call.__qualname__
            for dep in route.dependant.dependencies
        }
        assert any("PermissionChecker" in name for name in dependency_names), route.path


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/models"),
        ("post", "/models/import"),
        ("post", "/models/download"),
        ("put", "/config"),
        ("post", "/rebuild"),
    ],
)
def test_admin_operations_are_forbidden_for_non_admin_when_auth_is_enforced(
    monkeypatch: pytest.MonkeyPatch, method: str, path: str
) -> None:
    """The route-level dependency must reject a normal principal before mutation."""
    from StAgent.services.auth import deps

    monkeypatch.setattr(deps, "_auth_enforced", lambda: True)
    monkeypatch.setattr(deps, "_bearer_token", lambda _request: "token")
    monkeypatch.setattr(
        deps,
        "_principal_from_token",
        lambda _token: deps.UserPrincipal(
            is_superuser=False,
            roles=frozenset({"user"}),
            permissions=frozenset(),
        ),
    )
    app = FastAPI()
    app.include_router(semantic_api.router)
    with TestClient(app, raise_server_exceptions=False) as client:
        body = {"source_directory": "C:/model", "display_name": "model"}
        if method == "post" and path == "/models/download":
            body = {"repo_id": "owner/repo"}
        if method == "put":
            body = {"enabled": False}
        response = client.get(path) if method == "get" else getattr(client, method)(path, json=body)
    assert response.status_code == 403
