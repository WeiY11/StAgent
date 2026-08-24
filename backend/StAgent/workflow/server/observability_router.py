"""REST + WebSocket endpoints for the execution observability centre.

Mounted under ``/api/v1/workflow/observability``. Every endpoint is read-only:

- ``GET /gantt/{prompt_id}``            — Gantt chart for a run (live or replayed)
- ``GET /gantt/by-execution/{id}``      — same chart, addressed by execution id
- ``GET /live``                         — cross-workflow monitoring wall
- ``GET /profile/{prompt_id}``          — bottleneck / time-composition analysis
- ``GET /executions``                   — global, cross-flow execution history
- ``WS  /stream/{prompt_id}``           — per-prompt event stream for the chart

Charts come from the in-memory :class:`~StAgent.workflow.observability.ObservabilityHub`
while a run is live or recent, and are rebuilt from the persisted
``execution_history`` once it has been evicted — so a deep link keeps working
long after the process forgot the run.

Ownership: every path resolves the underlying ``workflow_executions`` row and
rejects a caller who does not own it, reusing the same rule as
``flows_router``/``router``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

import structlog
from fastapi import APIRouter, HTTPException, Query, WebSocket

from StAgent.services.auth import CurrentUserId
from StAgent.workflow.observability import (
    ExecutionGantt,
    GanttBuilder,
    build_profile,
    get_observability_hub,
)

from .event_bus import get_event_bus
from .ws import stream_execution
from .ws_auth import authenticate_websocket

logger = structlog.get_logger(__name__)

observability_router = APIRouter(prefix="/observability", tags=["workflow-observability"])

#: DB-side statuses that mean "still in flight".
_ACTIVE_DB_STATUSES = frozenset(
    {"pending", "queued", "running", "paused", "waiting_human"}
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _service() -> Any:
    from StAgent.services.service_manager import get_service_manager

    sm = get_service_manager()
    if sm.workflow_service is None:
        raise HTTPException(status_code=503, detail="Workflow service unavailable")
    return sm.workflow_service


def _assert_owner(record: dict[str, Any] | None, user_id: UUID) -> None:
    if record is None:
        return
    owner = record.get("user_id")
    if owner is not None and str(owner) != str(user_id):
        raise HTTPException(status_code=403, detail="Access denied to this execution")


async def _chart_for_record(
    prompt_id: str, record: dict[str, Any] | None
) -> ExecutionGantt | None:
    """Prefer the live in-memory chart, else replay the persisted history.

    A live chart is richer (node labels, cache/skip colouring, open bars), so it
    wins whenever the hub still has one. Once evicted, ``execution_history`` is
    the durable fallback — degrading to an approximate timeline for rows written
    before node timestamps were recorded.
    """
    chart = get_observability_hub().gantt(prompt_id)
    if chart is not None and chart.bars:
        if record is not None:
            chart.execution_id = chart.execution_id or str(record.get("id") or "") or None
            chart.flow_id = chart.flow_id or record.get("flow_id")
            chart.flow_name = chart.flow_name or record.get("flow_name")
        return chart
    if record is None:
        return chart
    try:
        return GanttBuilder.from_execution_history(record)
    except Exception:  # noqa: BLE001 - a broken row must not 500 the page
        logger.warning(
            "observability_replay_failed", prompt_id=prompt_id, exc_info=True
        )
        return chart


async def _record_for_prompt(prompt_id: str) -> dict[str, Any] | None:
    service = _service()
    record = await service.get_by_prompt_id(prompt_id)
    if record is not None:
        await _attach_flow_name(service, record)
    return record


async def _attach_flow_name(service: Any, record: dict[str, Any]) -> None:
    """Best-effort flow-name lookup for the chart header."""
    if record.get("flow_name") or not record.get("flow_id"):
        return
    try:
        from sqlmodel import select

        from StAgent.db.models.flow import Flow

        async with service._db.session() as session:  # noqa: SLF001 - same package
            result = await session.exec(
                select(Flow.name).where(Flow.id == UUID(str(record["flow_id"])))
            )
            record["flow_name"] = result.first()
    except Exception:  # noqa: BLE001 - cosmetic
        logger.debug("observability_flow_name_lookup_failed", exc_info=True)


def _parse_dt(value: str | None, field: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail=f"Invalid ISO-8601 timestamp for {field}: {value}"
        ) from exc


# ---------------------------------------------------------------------------
# Gantt
# ---------------------------------------------------------------------------


@observability_router.get("/gantt/{prompt_id}")
async def get_gantt(prompt_id: str, user_id: CurrentUserId) -> dict[str, Any]:
    """Gantt chart for one execution, addressed by engine prompt id."""
    record = await _record_for_prompt(prompt_id)
    _assert_owner(record, user_id)
    chart = await _chart_for_record(prompt_id, record)
    if chart is None:
        raise HTTPException(status_code=404, detail="No timeline for this prompt")
    return chart.to_dict()


@observability_router.get("/gantt/by-execution/{execution_id}")
async def get_gantt_by_execution(
    execution_id: UUID, user_id: CurrentUserId
) -> dict[str, Any]:
    """Same chart as :func:`get_gantt`, for clients holding an execution id."""
    service = _service()
    record = await service.get_execution(execution_id)
    if not record:
        raise HTTPException(status_code=404, detail="Execution not found")
    _assert_owner(record, user_id)
    await _attach_flow_name(service, record)
    prompt_id = str(record.get("prompt_id") or "")
    chart = await _chart_for_record(prompt_id, record)
    if chart is None:
        raise HTTPException(status_code=404, detail="No timeline for this execution")
    return chart.to_dict()


# ---------------------------------------------------------------------------
# Live monitoring wall
# ---------------------------------------------------------------------------


@observability_router.get("/live")
async def list_live(
    user_id: CurrentUserId,
    limit: int = Query(50, ge=1, le=200),
) -> dict[str, Any]:
    """Every execution of the caller's that has not reached a terminal state.

    Sourced from the database (so ownership filtering is authoritative and runs
    that predate this process still appear) and overlaid with the in-memory hub
    for current nodes, progress and a mini timeline. ``queue_position`` stays
    ``None`` on the default in-process configuration, which has no broker queue.
    """
    service = _service()
    rows, total = await service.list_all_executions(
        user_id=user_id, limit=limit, offset=0
    )
    active = [r for r in rows if str(r.get("status") or "") in _ACTIVE_DB_STATUSES]
    hub = get_observability_hub()

    items: list[dict[str, Any]] = []
    for record in active:
        prompt_id = str(record.get("prompt_id") or "")
        chart = hub.gantt(prompt_id) if prompt_id else None
        open_bars = [b for b in chart.bars if b.open] if chart else []
        done = len({b.node_id for b in chart.bars if not b.open}) if chart else 0
        queue_position = None
        if prompt_id:
            try:
                queue_position = await service.queue_position(prompt_id)
            except Exception:  # noqa: BLE001 - optional broker feature
                queue_position = None
        items.append(
            {
                "prompt_id": prompt_id or None,
                "execution_id": record.get("id"),
                "flow_id": record.get("flow_id"),
                "flow_name": record.get("flow_name"),
                "status": record.get("status"),
                "trigger_type": record.get("trigger_type"),
                "started_at_ms": chart.started_at_ms if chart else None,
                "started_at": record.get("started_at") or record.get("created_at"),
                "current_nodes": [b.node_id for b in open_bars]
                or ([record["current_node"]] if record.get("current_node") else []),
                "node_total": max(int(record.get("node_count") or 0), done + len(open_bars)),
                "node_done": done,
                "queue_position": queue_position,
                # Enough of the chart for a card-sized sparkline; the full chart
                # is one request away via /gantt/{prompt_id}.
                "gantt": chart.to_dict() if chart else None,
            }
        )
    return {"items": items, "total": len(items), "scanned": total}


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------


@observability_router.get("/profile/{prompt_id}")
async def get_profile(prompt_id: str, user_id: CurrentUserId) -> dict[str, Any]:
    """Bottleneck node, human-wait time, cache savings and parallelism."""
    record = await _record_for_prompt(prompt_id)
    _assert_owner(record, user_id)
    chart = await _chart_for_record(prompt_id, record)
    if chart is None:
        raise HTTPException(status_code=404, detail="No timeline for this prompt")
    return build_profile(chart).to_dict()


# ---------------------------------------------------------------------------
# Global execution history
# ---------------------------------------------------------------------------


@observability_router.get("/executions")
async def list_executions(
    user_id: CurrentUserId,
    status: str | None = Query(None, description="Exact execution status match"),
    flow_id: UUID | None = Query(None),
    trigger_type: str | None = Query(
        None, description="manual | cron | subworkflow | chat | webhook"
    ),
    q: str | None = Query(None, description="Substring over flow name / prompt id / execution id"),
    started_after: str | None = Query(None, description="ISO-8601 lower bound"),
    started_before: str | None = Query(None, description="ISO-8601 upper bound"),
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    """Cross-flow execution history — the global counterpart to the per-flow list."""
    service = _service()
    rows, total = await service.list_all_executions(
        user_id=user_id,
        status=status,
        flow_id=flow_id,
        trigger_type=trigger_type,
        q=q,
        started_after=_parse_dt(started_after, "started_after"),
        started_before=_parse_dt(started_before, "started_before"),
        limit=limit,
        offset=offset,
    )
    return {
        "items": [_execution_row(r) for r in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


def _execution_row(record: dict[str, Any]) -> dict[str, Any]:
    """Trim a persisted row to what the history table renders.

    Inputs/outputs and the full node history are deliberately omitted — a list
    of 200 executions must not drag every node payload across the wire.
    """
    return {
        "id": record.get("id"),
        "prompt_id": record.get("prompt_id"),
        "flow_id": record.get("flow_id"),
        "flow_name": record.get("flow_name"),
        "cron_job_id": record.get("cron_job_id"),
        "cron_job_name": record.get("cron_job_name"),
        "status": record.get("status"),
        "trigger_type": record.get("trigger_type"),
        "node_count": record.get("node_count"),
        "duration_ms": record.get("duration_ms"),
        "error": record.get("error"),
        "started_at": record.get("started_at"),
        "completed_at": record.get("completed_at"),
        "created_at": record.get("created_at"),
    }


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------


@observability_router.websocket("/stream/{prompt_id}")
async def ws_observability_stream(websocket: WebSocket, prompt_id: str) -> None:
    """Per-prompt event stream for the Gantt view.

    Deliberately identical in payload to ``WS /ws/executions/{prompt_id}`` — the
    separate path lets the observability centre subscribe without competing with
    the editor overlay's own ref-counted socket, and keeps the existing endpoint
    free to evolve on its own.
    """
    auth = await authenticate_websocket(websocket)
    if auth is None:
        return
    user_id, subprotocol = auth
    service = _service()
    record = await service.get_by_prompt_id(prompt_id)
    if record is not None:
        owner = record.get("user_id")
        if owner is not None and str(owner) != str(user_id):
            await websocket.close(code=4003, reason="Access denied")
            return
    from StAgent.services.service_manager import get_service_manager

    bus = await get_event_bus(get_service_manager())
    await stream_execution(websocket, prompt_id, bus, subprotocol=subprotocol)
