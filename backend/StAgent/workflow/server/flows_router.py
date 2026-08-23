"""Flow CRUD and execution history routes for the workflow server.

These endpoints replace the legacy ``/api/v1/flows`` module. They live at
``/api/v1/workflow/flows`` and ``/api/v1/workflow/executions`` and share the
same :class:`APIRouter` as the engine-level ``/prompts`` routes so the whole
workflow feature lives behind a single prefix.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import update as sa_update
from sqlmodel import col, select

from StAgent.db import DatabaseService, get_database_service
from StAgent.db.models import (
    Flow,
    FlowRead,
    FlowStatus,
    FlowType,
)
from StAgent.db.models.base import utc_now
from StAgent.db.sqlite_compat import load_entity_by_id, uuid_column_equals
from StAgent.schema.api import PaginatedResponse
from StAgent.services.auth import CurrentUserId
from StAgent.services.auth.deps import principal_from_request_state

from ..io.loader import WorkflowLoaderError
from ..io.loader import load as load_document
from .flow_permissions import can_access_flow, require_flow_access
from .schemas import (
    FlowDuplicateResponse,
    WorkflowExecutionDetail,
    WorkflowExecutionSummary,
)


def _canonicalize(data: dict[str, Any] | None) -> dict[str, Any] | None:
    """Validate incoming flow data against the canonical document shape.

    The editor saves canonical documents (``nodes`` dict + ``control`` +
    sibling ``ui`` layout block). Anything else is rejected — there is no
    legacy conversion path on write.
    """
    if data is None:
        return None
    if not isinstance(data, dict):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Flow data must be a canonical workflow document object",
        )
    try:
        load_document({k: v for k, v in data.items() if k != "ui"})
    except WorkflowLoaderError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Flow data is not a canonical workflow document: {exc}",
        ) from exc
    return data

logger = structlog.get_logger(__name__)

flows_router = APIRouter(tags=["workflow-flows"])


# ---------------------------------------------------------------------------
# Request models (kept compact; mirrors the legacy schema one-to-one)
# ---------------------------------------------------------------------------


class FlowCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    icon: str | None = Field(default="🤖", max_length=50)
    icon_bg_color: str | None = Field(default=None, max_length=20)
    status: FlowStatus = Field(default=FlowStatus.DRAFT)
    flow_type: FlowType = Field(default=FlowType.AGENT)
    is_public: bool = Field(default=False)
    tags: str | None = Field(default=None, max_length=500)
    data: dict[str, Any] | None = None
    settings: dict[str, Any] | None = None
    folder_id: UUID | None = None


class FlowUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    icon: str | None = Field(default=None, max_length=50)
    icon_bg_color: str | None = Field(default=None, max_length=20)
    status: FlowStatus | None = None
    flow_type: FlowType | None = None
    is_public: bool | None = None
    tags: str | None = Field(default=None, max_length=500)
    data: dict[str, Any] | None = None
    settings: dict[str, Any] | None = None
    folder_id: UUID | None = None


class RecentFlowItem(BaseModel):
    id: str
    name: str
    description: str | None
    status: str
    nodeCount: int
    createdAt: str
    updatedAt: str


class WorkflowExecutionListResponse(BaseModel):
    executions: list[WorkflowExecutionSummary]
    total: int


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_workflow_service() -> Any:
    from StAgent.services.service_manager import get_service_manager

    sm = get_service_manager()
    if sm.workflow_service is None:
        raise HTTPException(status_code=503, detail="Workflow service unavailable")
    return sm.workflow_service


def _node_count(flow: Flow) -> int:
    if not flow.data:
        return 0


async def _load_flow(session: Any, flow_id: UUID) -> Flow | None:
    return await load_entity_by_id(session, Flow, flow_id, parent_table="flows")


async def _update_flow_row(
    session: Any,
    flow_id: UUID,
    flow: Flow,
    values: dict[str, Any],
) -> Flow:
    """Update a flow by UUID-compatible predicate; fake unit sessions mutate in place."""
    execute = getattr(session, "execute", None)
    if callable(execute):
        await session.execute(
            sa_update(Flow)
            .where(uuid_column_equals(session, Flow.id, flow_id))
            .values(**values)
        )
        reloaded = await _load_flow(session, flow_id)
        return reloaded or flow

    for key, value in values.items():
        setattr(flow, key, value)
    add = getattr(session, "add", None)
    if callable(add):
        add(flow)
    flush = getattr(session, "flush", None)
    if callable(flush):
        await flush()
    refresh = getattr(session, "refresh", None)
    if callable(refresh):
        await refresh(flow)
    return flow
    try:
        doc = json.loads(flow.data)
        nodes = doc.get("nodes") if isinstance(doc, dict) else None
        return len(nodes) if isinstance(nodes, dict) else 0
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Flow CRUD
# ---------------------------------------------------------------------------


@flows_router.post(
    "/flows",
    response_model=FlowRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_flow(
    data: FlowCreateRequest,
    user_id: CurrentUserId,
    db: Annotated[DatabaseService, Depends(get_database_service)],
) -> FlowRead:
    async with db.session() as session:
        canonical = _canonicalize(data.data)
        flow = Flow(
            name=data.name,
            description=data.description,
            icon=data.icon,
            icon_bg_color=data.icon_bg_color,
            status=data.status,
            flow_type=data.flow_type,
            is_public=data.is_public,
            tags=data.tags,
            data=json.dumps(canonical) if canonical else None,
            settings=json.dumps(data.settings) if data.settings else None,
            folder_id=data.folder_id,
            user_id=user_id,
        )
        session.add(flow)
        await session.flush()
        await session.refresh(flow)
        return FlowRead.model_validate(flow)


@flows_router.get("/flows", response_model=PaginatedResponse[FlowRead])
async def list_flows(
    request: Request,
    user_id: CurrentUserId,
    db: Annotated[DatabaseService, Depends(get_database_service)],
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    status_: FlowStatus | None = Query(default=None, alias="status"),
    flow_type: FlowType | None = Query(default=None),
    folder_id: UUID | None = Query(default=None),
    is_public: bool | None = Query(default=None),
    search: str | None = Query(default=None, max_length=100),
) -> PaginatedResponse[FlowRead]:
    principal = principal_from_request_state(request, user_id)
    async with db.session() as session:
        query = select(Flow).where(Flow.is_deleted == False)  # noqa: E712
        if status_ is not None:
            query = query.where(Flow.status == status_)
        if flow_type is not None:
            query = query.where(Flow.flow_type == flow_type)
        if folder_id is not None:
            query = query.where(uuid_column_equals(session, Flow.folder_id, folder_id))
        if is_public is not None:
            query = query.where(Flow.is_public == is_public)
        if search:
            query = query.where(
                (Flow.name.ilike(f"%{search}%"))
                | (Flow.description.ilike(f"%{search}%"))
            )

        query = query.order_by(col(Flow.updated_at).desc())
        visible_flows = [
            flow
            for flow in (await session.exec(query)).all()
            if can_access_flow(flow, principal, "view")
        ]
        total = len(visible_flows)
        start = (page - 1) * page_size
        flows = visible_flows[start : start + page_size]

        return PaginatedResponse[FlowRead](
            items=[FlowRead.model_validate(f) for f in flows],
            total=total,
            page=page,
            page_size=page_size,
            has_next=(page * page_size) < total,
            has_prev=page > 1,
        )


@flows_router.get("/flows/recent", response_model=list[RecentFlowItem])
async def get_recent_flows(
    request: Request,
    user_id: CurrentUserId,
    db: Annotated[DatabaseService, Depends(get_database_service)],
    limit: int = Query(default=10, ge=1, le=50),
) -> list[RecentFlowItem]:
    principal = principal_from_request_state(request, user_id)
    async with db.session() as session:
        query = (
            select(Flow)
            .where(
                Flow.is_deleted == False,  # noqa: E712
            )
            .order_by(col(Flow.updated_at).desc())
        )
        flows = [
            flow
            for flow in (await session.exec(query)).all()
            if can_access_flow(flow, principal, "view")
        ][:limit]

    return [
        RecentFlowItem(
            id=str(f.id),
            name=f.name,
            description=f.description,
            status=f.status.value,
            nodeCount=_node_count(f),
            createdAt=f.created_at.isoformat() if f.created_at else "",
            updatedAt=f.updated_at.isoformat() if f.updated_at else "",
        )
        for f in flows
    ]


@flows_router.get("/flows/{flow_id}", response_model=FlowRead)
async def get_flow(
    flow_id: UUID,
    request: Request,
    user_id: CurrentUserId,
    db: Annotated[DatabaseService, Depends(get_database_service)],
) -> FlowRead:
    principal = principal_from_request_state(request, user_id)
    async with db.session() as session:
        flow = await _load_flow(session, flow_id)
        if not flow or flow.is_deleted:
            raise HTTPException(status_code=404, detail="Flow not found")
        require_flow_access(flow, principal, "view")
        return FlowRead.model_validate(flow)


@flows_router.patch("/flows/{flow_id}", response_model=FlowRead)
@flows_router.put("/flows/{flow_id}", response_model=FlowRead)
async def update_flow(
    flow_id: UUID,
    data: FlowUpdateRequest,
    request: Request,
    user_id: CurrentUserId,
    db: Annotated[DatabaseService, Depends(get_database_service)],
) -> FlowRead:
    principal = principal_from_request_state(request, user_id)
    async with db.session() as session:
        flow = await _load_flow(session, flow_id)
        if not flow or flow.is_deleted:
            raise HTTPException(status_code=404, detail="Flow not found")
        require_flow_access(flow, principal, "edit")

        update_data = data.model_dump(exclude_unset=True)
        if "data" in update_data:
            canonical = _canonicalize(update_data["data"])
            update_data["data"] = (
                json.dumps(canonical) if canonical else None
            )
        if "settings" in update_data:
            update_data["settings"] = (
                json.dumps(update_data["settings"]) if update_data["settings"] else None
            )

        update_data["version"] = (flow.version or 0) + 1
        update_data["updated_at"] = utc_now()
        updated = await _update_flow_row(session, flow_id, flow, update_data)
        return FlowRead.model_validate(updated)


@flows_router.delete("/flows/{flow_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_flow(
    flow_id: UUID,
    request: Request,
    user_id: CurrentUserId,
    db: Annotated[DatabaseService, Depends(get_database_service)],
) -> None:
    principal = principal_from_request_state(request, user_id)
    async with db.session() as session:
        flow = await _load_flow(session, flow_id)
        if not flow or flow.is_deleted:
            raise HTTPException(status_code=404, detail="Flow not found")
        require_flow_access(flow, principal, "delete")

        now = utc_now()
        await _update_flow_row(
            session,
            flow_id,
            flow,
            {"is_deleted": True, "deleted_at": now, "updated_at": now},
        )


@flows_router.post(
    "/flows/{flow_id}/duplicate",
    response_model=FlowDuplicateResponse,
    status_code=status.HTTP_201_CREATED,
)
async def duplicate_flow(
    flow_id: UUID,
    request: Request,
    user_id: CurrentUserId,
    db: Annotated[DatabaseService, Depends(get_database_service)],
) -> FlowDuplicateResponse:
    principal = principal_from_request_state(request, user_id)
    async with db.session() as session:
        flow = await _load_flow(session, flow_id)
        if not flow or flow.is_deleted:
            raise HTTPException(status_code=404, detail="Flow not found")
        require_flow_access(flow, principal, "view")

        clone = Flow(
            name=f"{flow.name} (copy)",
            description=flow.description,
            icon=flow.icon,
            icon_bg_color=flow.icon_bg_color,
            status=FlowStatus.DRAFT,
            flow_type=flow.flow_type,
            is_public=False,
            tags=flow.tags,
            data=flow.data,
            settings=flow.settings,
            folder_id=flow.folder_id,
            user_id=user_id,
        )
        session.add(clone)
        await session.flush()
        await session.refresh(clone)
        return FlowDuplicateResponse(flow_id=clone.id, name=clone.name)


# ---------------------------------------------------------------------------
# Execution history by execution id (flow-scoped history is in ``router.py``)
# ---------------------------------------------------------------------------


@flows_router.get(
    "/executions/{execution_id}",
    response_model=WorkflowExecutionDetail,
)
async def get_execution(
    execution_id: UUID,
    user_id: CurrentUserId,
) -> WorkflowExecutionDetail:
    service = _get_workflow_service()
    record = await service.get_execution(execution_id)
    if not record:
        raise HTTPException(status_code=404, detail="Execution not found")
    owner = record.get("user_id")
    if owner is not None and str(owner) != str(user_id):
        raise HTTPException(status_code=403, detail="Access denied to this execution")

    return WorkflowExecutionDetail(
        id=record["id"],
        flow_id=record.get("flow_id"),
        status=record["status"],
        trigger_type=record.get("trigger_type", "manual"),
        node_count=record.get("node_count", 0),
        duration_ms=record.get("duration_ms", 0),
        error=record.get("error"),
        current_node=record.get("current_node"),
        inputs=record.get("inputs", {}),
        outputs=record.get("outputs", {}),
        execution_history=record.get("execution_history", []),
        started_at=_opt_dt(record.get("started_at")),
        completed_at=_opt_dt(record.get("completed_at")),
        created_at=_opt_dt(record.get("created_at")) or datetime.utcnow(),
    )


@flows_router.post("/executions/{execution_id}/cancel")
async def cancel_execution(
    execution_id: UUID,
    user_id: CurrentUserId,
) -> dict[str, Any]:
    service = _get_workflow_service()
    record = await service.get_execution(execution_id)
    if not record:
        raise HTTPException(status_code=404, detail="Execution not found")
    owner = record.get("user_id")
    if owner is not None and str(owner) != str(user_id):
        raise HTTPException(status_code=403, detail="Access denied to this execution")
    ok = await service.cancel_execution(execution_id)
    if not ok:
        raise HTTPException(status_code=400, detail="Execution not cancellable")
    return {"execution_id": str(execution_id), "status": "cancelled"}


@flows_router.post("/executions/{execution_id}/pause")
async def pause_execution(
    execution_id: UUID,
    user_id: CurrentUserId,
) -> dict[str, Any]:
    service = _get_workflow_service()
    record = await service.get_execution(execution_id)
    if not record:
        raise HTTPException(status_code=404, detail="Execution not found")
    owner = record.get("user_id")
    if owner is not None and str(owner) != str(user_id):
        raise HTTPException(status_code=403, detail="Access denied to this execution")
    ok = await service.pause_execution(execution_id)
    if not ok:
        raise HTTPException(status_code=400, detail="Execution not pausable")
    return {"execution_id": str(execution_id), "status": "paused"}


@flows_router.post("/executions/{execution_id}/resume")
async def resume_execution(
    execution_id: UUID,
    flow_id: UUID,
    user_id: CurrentUserId,
    resume_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    service = _get_workflow_service()
    record = await service.get_execution(execution_id)
    if not record:
        raise HTTPException(status_code=404, detail="Execution not found")
    owner = record.get("user_id")
    if owner is not None and str(owner) != str(user_id):
        raise HTTPException(status_code=403, detail="Access denied to this execution")
    result = await service.resume_execution(execution_id, flow_id, resume_data)
    if not result:
        raise HTTPException(status_code=400, detail="Execution not resumable")
    return {"execution_id": str(execution_id), "status": result.status.value}


def _opt_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(value)
    except Exception:
        return None
