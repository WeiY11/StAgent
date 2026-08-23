"""Administrative lifecycle API for the opt-in local semantic projection."""

from __future__ import annotations

import asyncio
import os
import shutil
import uuid
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from StAgent.config.config import load_config, save_config
from StAgent.config.constants import StAgent_HOME
from StAgent.knowledge.local_models import LocalModelRegistry, ModelValidationError
from StAgent.services.auth.deps import get_current_principal, require_permissions
from StAgent.services.service_manager import get_service_manager

router = APIRouter()
_admin = [Depends(require_permissions("admin:panel"))]


class ModelImportBody(BaseModel):
    source_directory: str = Field(min_length=1, max_length=4096)
    display_name: str = Field(min_length=1, max_length=200)
    query_prompt: str | None = Field(default=None, max_length=1000)
    document_prompt: str | None = Field(default=None, max_length=1000)
    normalize: bool = True


class ModelDownloadBody(BaseModel):
    repo_id: str = Field(min_length=3, max_length=193)
    revision: str | None = Field(default=None, max_length=128)
    display_name: str | None = Field(default=None, max_length=200)
    query_prompt: str | None = Field(default=None, max_length=1000)
    document_prompt: str | None = Field(default=None, max_length=1000)
    normalize: bool = True


class SemanticConfigBody(BaseModel):
    enabled: bool
    vector_backend: str = Field(default="sqlite-vec", max_length=64)
    local_model_id: str = Field(default="", max_length=128)
    auto_reindex: bool = True


def _registry() -> LocalModelRegistry:
    import os

    return LocalModelRegistry(Path(os.getenv("StAgent_HOME", str(StAgent_HOME))))


def _safe_model(record: Any) -> dict[str, Any]:
    return {
        "model_id": record.model_id,
        "display_name": record.display_name,
        "source_type": record.source_type,
        "resolved_revision": record.resolved_revision or None,
        "dimension": record.dimension,
        "query_prompt_configured": bool(record.query_prompt),
        "document_prompt_configured": bool(record.document_prompt),
        "normalize": record.normalize,
        "file_size": record.file_size,
        "status": record.status,
        "fingerprint": record.fingerprint,
    }


@router.get("/status")
async def semantic_status(_: Annotated[Any, Depends(get_current_principal)]) -> dict[str, Any]:
    return get_service_manager().knowledge_semantic_status


@router.get("/models", dependencies=_admin)
async def list_models() -> dict[str, Any]:
    return {"models": [_safe_model(row) for row in _registry().list()]}


@router.post("/models/import", dependencies=_admin, status_code=status.HTTP_201_CREATED)
async def import_model(body: ModelImportBody) -> dict[str, Any]:
    try:
        record = await asyncio.to_thread(
            _registry().import_local,
            body.source_directory,
            display_name=body.display_name,
            query_prompt=body.query_prompt,
            document_prompt=body.document_prompt,
            normalize=body.normalize,
        )
        return {"model": _safe_model(record)}
    except (ModelValidationError, OSError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/models/download", dependencies=_admin, status_code=status.HTTP_201_CREATED)
async def download_model(body: ModelDownloadBody) -> dict[str, Any]:
    try:
        record = await asyncio.to_thread(
            _registry().download_huggingface,
            body.repo_id,
            revision=body.revision,
            display_name=body.display_name,
            query_prompt=body.query_prompt,
            document_prompt=body.document_prompt,
            normalize=body.normalize,
        )
        return {"model": _safe_model(record)}
    except (ModelValidationError, OSError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/models/{model_id}", dependencies=_admin)
async def delete_model(model_id: str) -> dict[str, bool]:
    sm = get_service_manager()
    semantic = sm.knowledge_semantic_status
    active_versions = (str(semantic.get("active_version") or ""), str(semantic.get("candidate_version") or ""))
    registry = _registry()
    record = registry.get(model_id)
    if record is None:
        raise HTTPException(status_code=404, detail="model_not_found")
    registry_model_marker = f"semantic-{record.model_id.replace('local-', '')[:16]}-"
    if model_id == semantic.get("model_id") or any(
        record.fingerprint[:12] in version or version.startswith(registry_model_marker) for version in active_versions
    ):
        raise HTTPException(status_code=409, detail="active_or_candidate_model")
    target = registry.models_dir / model_id
    if target.parent.resolve() != registry.models_dir.resolve() or target.is_symlink():
        raise HTTPException(status_code=400, detail="invalid_model_path")
    # Move first so a catalog-write failure leaves the original model intact.
    staged = registry.staging_dir / f"deleting-{uuid.uuid4().hex}"
    os.replace(target, staged)
    try:
        catalog = registry._read_catalog()  # noqa: SLF001 - registry owns the validated catalog shape
        del catalog[model_id]
        registry._write_catalog(catalog)  # noqa: SLF001
    except Exception:
        os.replace(staged, target)
        raise
    await asyncio.to_thread(shutil.rmtree, staged)
    return {"deleted": True}


@router.put("/config", dependencies=_admin)
async def update_semantic_config(body: SemanticConfigBody) -> dict[str, Any]:
    sm = get_service_manager()
    if sm.knowledge_semantic_status.get("environment_managed"):
        raise HTTPException(status_code=409, detail="managed_by_environment")
    if body.enabled and int(getattr(sm.settings, "workers", 1) or 1) > 1:
        raise HTTPException(
            status_code=409,
            detail="local_backend_requires_single_worker_use_milvus",
        )
    if body.enabled and not body.local_model_id:
        raise HTTPException(status_code=400, detail="local_model_required")
    backend = body.vector_backend.strip().lower().replace("_", "-")
    if backend not in {"sqlite-vec", "sqlitevec", "local"}:
        raise HTTPException(status_code=400, detail="local_vector_backend_must_be_sqlite_vec")
    if body.enabled:
        registry = _registry()
        record = registry.get(body.local_model_id)
        model_path = registry.models_dir / body.local_model_id
        if (
            record is None
            or record.status != "ready"
            or not record.dimension
            or model_path.parent.resolve() != registry.models_dir.resolve()
            or model_path.is_symlink()
            or not model_path.is_dir()
        ):
            raise HTTPException(status_code=400, detail="local_model_not_ready")
    previous_task = sm.knowledge_semantic_status.get("task_id")
    if previous_task and sm.db is not None and sm.task_manager is not None:
        async with sm.db.session() as session:
            await sm.task_manager.kill_task(session, str(previous_task))
    config = load_config()
    config.knowledge.semantic.enabled = body.enabled
    config.knowledge.semantic.vector_backend = backend
    config.knowledge.semantic.local_model_id = body.local_model_id
    config.knowledge.semantic.auto_reindex = body.auto_reindex
    save_config(config)
    await sm._start_knowledge_retrieval()
    if body.enabled:
        await sm.enqueue_knowledge_vector_rebuild()
    return sm.knowledge_semantic_status


@router.post("/rebuild", dependencies=_admin, status_code=status.HTTP_202_ACCEPTED)
async def rebuild_semantic() -> dict[str, Any]:
    task_id = await get_service_manager().enqueue_knowledge_vector_rebuild()
    if task_id is None:
        raise HTTPException(status_code=409, detail="semantic_rebuild_unavailable")
    return {"task_id": task_id}
