"""Agent running-trace API (debug / eval / model comparison)."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Response, status
from pydantic import BaseModel, Field

from StAgent.api.v1.chat_deps import ChatSvc
from StAgent.services.auth import CurrentUserId
from StAgent.telemetry.trace.export import build_span_tree, export_trace_jsonl
from StAgent.telemetry.trace.models import loads_json, new_experiment_id
from StAgent.telemetry.trace.store import get_trace_store
from StAgent.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter()


class TraceSummary(BaseModel):
    trace_id: str
    parent_trace_id: str | None = None
    session_id: str | None = None
    user_id: str | None = None
    scope: str = "chat_turn"
    agent_name: str = ""
    model: str = ""
    status: str = "running"
    terminal_reason: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    latency_ms: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_miss_tokens: int = 0
    total_cost_usd: float = 0.0
    tool_call_count: int = 0
    llm_call_count: int = 0
    experiment_id: str | None = None
    prompt_hash: str | None = None
    tags: Any = None
    error: str | None = None
    scores: Any = None
    root_span_id: str | None = None


class TraceSpanRead(BaseModel):
    span_id: str
    parent_span_id: str | None = None
    trace_id: str
    seq: int = 0
    kind: str = "event"
    name: str = ""
    status: str = "ok"
    started_at: datetime | None = None
    ended_at: datetime | None = None
    latency_ms: float = 0.0
    attrs: Any = None
    input_preview: str | None = None
    output_preview: str | None = None
    payload_ref: str | None = None


class TraceDetail(BaseModel):
    trace: TraceSummary
    spans: list[TraceSpanRead] = Field(default_factory=list)
    tree: list[dict[str, Any]] = Field(default_factory=list)


class ModelTraceStats(BaseModel):
    model: str
    runs: int = 0
    successes: int = 0
    errors: int = 0
    success_rate: float = 0.0
    avg_latency_ms: float = 0.0
    p50_latency_ms: float = 0.0
    p95_latency_ms: float = 0.0
    avg_input_tokens: float = 0.0
    avg_output_tokens: float = 0.0
    avg_cost_usd: float = 0.0
    avg_tool_calls: float = 0.0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cost_usd: float = 0.0


class ExperimentCreate(BaseModel):
    name: str = ""
    prompt: str
    model_ids: list[str] = Field(min_length=1)
    session_id: str | None = None
    agent_name: str = "default_agent"


class ExperimentRead(BaseModel):
    experiment_id: str
    name: str = ""
    prompt: str = ""
    session_id: str | None = None
    model_ids: list[str] = Field(default_factory=list)
    created_by: str | None = None
    status: str = "pending"
    error: str | None = None
    created_at: datetime | None = None
    traces: list[TraceSummary] = Field(default_factory=list)


def _summary(row: Any) -> TraceSummary:
    return TraceSummary(
        trace_id=row.trace_id,
        parent_trace_id=row.parent_trace_id,
        session_id=row.session_id,
        user_id=row.user_id,
        scope=row.scope,
        agent_name=row.agent_name,
        model=row.model,
        status=row.status,
        terminal_reason=row.terminal_reason,
        started_at=row.started_at,
        ended_at=row.ended_at,
        latency_ms=row.latency_ms,
        input_tokens=row.input_tokens,
        output_tokens=row.output_tokens,
        cache_read_tokens=row.cache_read_tokens,
        cache_miss_tokens=row.cache_miss_tokens,
        total_cost_usd=row.total_cost_usd,
        tool_call_count=row.tool_call_count,
        llm_call_count=row.llm_call_count,
        experiment_id=row.experiment_id,
        prompt_hash=row.prompt_hash,
        tags=loads_json(row.tags, default=None),
        error=row.error,
        scores=loads_json(row.scores, default=None),
        root_span_id=row.root_span_id,
    )


def _span(row: Any) -> TraceSpanRead:
    return TraceSpanRead(
        span_id=row.span_id,
        parent_span_id=row.parent_span_id,
        trace_id=row.trace_id,
        seq=row.seq,
        kind=row.kind,
        name=row.name,
        status=row.status,
        started_at=row.started_at,
        ended_at=row.ended_at,
        latency_ms=row.latency_ms,
        attrs=loads_json(row.attrs, default={}),
        input_preview=row.input_preview,
        output_preview=row.output_preview,
        payload_ref=row.payload_ref,
    )


def _same_user(left: object, right: object) -> bool:
    return bool(left) and str(left) == str(right)


async def _load_trace_for_user(trace_id: str, user_id: UUID) -> Any:
    store = get_trace_store()
    row = await store.get_trace(trace_id)
    if row is None or not _same_user(row.user_id, user_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Trace not found")
    return row


async def _load_experiment_for_user(experiment_id: str, user_id: UUID) -> Any:
    store = get_trace_store()
    row = await store.get_experiment(experiment_id)
    if row is None or not _same_user(row.created_by, user_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Experiment not found",
        )
    return row


async def _validate_experiment_session(
    session_id: str | None,
    *,
    user_id: UUID,
    chat_svc: ChatSvc,
) -> str | None:
    if not session_id:
        return None
    try:
        sid = UUID(str(session_id))
    except (TypeError, ValueError, AttributeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid session_id",
        ) from exc
    session = await chat_svc.get_session(sid, user_id=user_id)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Session not found",
        )
    return str(sid)


@router.get("", response_model=list[TraceSummary])
async def list_traces(
    user_id: CurrentUserId,
    session_id: str | None = None,
    model: str | None = None,
    status_filter: str | None = Query(default=None, alias="status"),
    experiment_id: str | None = None,
    has_error: bool | None = None,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[TraceSummary]:
    store = get_trace_store()
    rows = await store.list_traces(
        session_id=session_id,
        user_id=str(user_id),
        model=model,
        status=status_filter,
        experiment_id=experiment_id,
        has_error=has_error,
        limit=limit,
        offset=offset,
    )
    return [_summary(r) for r in rows]


@router.get("/stats/by-model", response_model=list[ModelTraceStats])
async def stats_by_model(
    user_id: CurrentUserId,
    days: int = Query(default=30, ge=1, le=365),
) -> list[ModelTraceStats]:
    store = get_trace_store()
    rows = await store.stats_by_model(days=days, user_id=str(user_id))
    return [ModelTraceStats(**row) for row in rows]


@router.get("/experiments", response_model=list[ExperimentRead])
async def list_experiments(
    user_id: CurrentUserId,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[ExperimentRead]:
    store = get_trace_store()
    rows = await store.list_experiments(
        created_by=str(user_id),
        limit=limit,
        offset=offset,
    )
    out: list[ExperimentRead] = []
    for row in rows:
        traces = await store.list_traces(
            experiment_id=row.experiment_id,
            user_id=str(user_id),
            limit=50,
        )
        out.append(
            ExperimentRead(
                experiment_id=row.experiment_id,
                name=row.name,
                prompt=row.prompt,
                session_id=row.session_id,
                model_ids=loads_json(row.model_ids, default=[]) or [],
                created_by=row.created_by,
                status=row.status,
                error=row.error,
                created_at=row.created_at,
                traces=[_summary(t) for t in traces],
            )
        )
    return out


@router.post("/experiments", response_model=ExperimentRead, status_code=status.HTTP_201_CREATED)
async def create_experiment(
    body: ExperimentCreate,
    user_id: CurrentUserId,
    chat_svc: ChatSvc,
) -> ExperimentRead:
    if not body.prompt.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="prompt required")
    if not body.model_ids:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="model_ids required")
    store = get_trace_store()
    eid = new_experiment_id()
    session_id = await _validate_experiment_session(
        body.session_id,
        user_id=user_id,
        chat_svc=chat_svc,
    )
    row = await store.create_experiment(
        experiment_id=eid,
        name=body.name or f"compare-{eid[:8]}",
        prompt=body.prompt,
        model_ids=list(body.model_ids),
        session_id=session_id,
        created_by=str(user_id) if user_id else None,
    )
    return ExperimentRead(
        experiment_id=row.experiment_id,
        name=row.name,
        prompt=row.prompt,
        session_id=row.session_id,
        model_ids=list(body.model_ids),
        created_by=row.created_by,
        status=row.status,
        error=row.error,
        created_at=row.created_at,
        traces=[],
    )


@router.get("/experiments/{experiment_id}", response_model=ExperimentRead)
async def get_experiment(experiment_id: str, user_id: CurrentUserId) -> ExperimentRead:
    store = get_trace_store()
    row = await _load_experiment_for_user(experiment_id, user_id)
    traces = await store.list_traces(
        experiment_id=experiment_id,
        user_id=str(user_id),
        limit=100,
    )
    return ExperimentRead(
        experiment_id=row.experiment_id,
        name=row.name,
        prompt=row.prompt,
        session_id=row.session_id,
        model_ids=loads_json(row.model_ids, default=[]) or [],
        created_by=row.created_by,
        status=row.status,
        error=row.error,
        created_at=row.created_at,
        traces=[_summary(t) for t in traces],
    )


@router.post("/experiments/{experiment_id}/run", response_model=ExperimentRead)
async def run_experiment(
    experiment_id: str,
    user_id: CurrentUserId,
    chat_svc: ChatSvc,
    agent_name: str = Query(default="default_agent"),
) -> ExperimentRead:
    """Run the experiment prompt once per configured model."""
    store = get_trace_store()
    row = await _load_experiment_for_user(experiment_id, user_id)
    model_ids = loads_json(row.model_ids, default=[]) or []
    if not model_ids:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="no models configured")

    await store.update_experiment(experiment_id, status="running", error=None)
    errors: list[str] = []

    try:
        from StAgent.runtime.definition import ModelPolicy
        from StAgent.sdk import AgentRuntime
        from StAgent.services.service_manager import get_service_manager

        sm = get_service_manager()
        runtime = AgentRuntime.from_service_manager(sm)
        session_uuid: UUID | None = None
        if row.session_id:
            validated_session = await _validate_experiment_session(
                row.session_id,
                user_id=user_id,
                chat_svc=chat_svc,
            )
            session_uuid = UUID(validated_session) if validated_session else None

        for model_id in model_ids:
            try:
                result = await runtime.run(
                    agent_name,
                    row.prompt,
                    session_id=session_uuid,
                    user_id=UUID(str(user_id)) if user_id else None,
                    tool_extra={
                        "experiment_id": experiment_id,
                        "tags": {"experiment": True, "model": model_id},
                    },
                    overrides={
                        "model": ModelPolicy(model=str(model_id)),
                    },
                )
                logger.info(
                    "trace_experiment_model_done",
                    experiment_id=experiment_id,
                    model=model_id,
                    reason=result.reason,
                    error=result.error,
                )
            except Exception as exc:
                logger.warning(
                    "trace_experiment_model_failed",
                    experiment_id=experiment_id,
                    model=model_id,
                    error=str(exc),
                )
                errors.append(f"{model_id}: {exc}")
    except Exception as exc:
        await store.update_experiment(
            experiment_id, status="error", error=str(exc)[:2000]
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"experiment run failed: {exc}",
        ) from exc

    status_val = "completed" if not errors else "completed_with_errors"
    await store.update_experiment(
        experiment_id,
        status=status_val,
        error="; ".join(errors) if errors else None,
    )
    return await get_experiment(experiment_id, user_id)


@router.get("/{trace_id}", response_model=TraceDetail)
async def get_trace(trace_id: str, user_id: CurrentUserId) -> TraceDetail:
    store = get_trace_store()
    row = await _load_trace_for_user(trace_id, user_id)
    spans = await store.list_spans(trace_id)
    return TraceDetail(
        trace=_summary(row),
        spans=[_span(s) for s in spans],
        tree=build_span_tree(spans),
    )


@router.get("/{trace_id}/spans", response_model=list[TraceSpanRead])
async def list_trace_spans(trace_id: str, user_id: CurrentUserId) -> list[TraceSpanRead]:
    store = get_trace_store()
    await _load_trace_for_user(trace_id, user_id)
    spans = await store.list_spans(trace_id)
    return [_span(s) for s in spans]


class TraceSpanDetail(TraceSpanRead):
    """Span plus the full (untruncated) input/output payloads."""

    input_full: str | None = None
    output_full: str | None = None


def _load_span_payload(payload_ref: str) -> dict[str, Any] | None:
    """Read the span payload file, refusing paths outside ``TRACES_DIR``."""
    from pathlib import Path

    import json

    from StAgent.config.constants import TRACES_DIR

    try:
        base = Path(TRACES_DIR).resolve()
        path = Path(payload_ref).resolve()
        if base != path and base not in path.parents:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _payload_field(payload: dict[str, Any] | None, key: str) -> str | None:
    """Stringify one payload field (payloads store arbitrary JSON values)."""
    if not payload:
        return None
    import json

    value = payload.get(key)
    if value is None:
        return None
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


@router.get("/{trace_id}/spans/{span_id}", response_model=TraceSpanDetail)
async def get_trace_span(trace_id: str, span_id: str, user_id: CurrentUserId) -> TraceSpanDetail:
    """Return one span with full payloads (payload file when present, preview fallback)."""
    store = get_trace_store()
    await _load_trace_for_user(trace_id, user_id)
    spans = await store.list_spans(trace_id)
    row = next((s for s in spans if s.span_id == span_id), None)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Span not found")
    payload = _load_span_payload(row.payload_ref) if row.payload_ref else None
    return TraceSpanDetail(
        **_span(row).model_dump(),
        input_full=_payload_field(payload, "input") or row.input_preview,
        output_full=_payload_field(payload, "output") or row.output_preview,
    )


@router.get("/{trace_id}/export")
async def export_trace(trace_id: str, user_id: CurrentUserId) -> Response:
    store = get_trace_store()
    await _load_trace_for_user(trace_id, user_id)
    try:
        body = await export_trace_jsonl(trace_id, store=store)
    except KeyError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Trace not found") from None
    return Response(
        content=body,
        media_type="application/x-ndjson",
        headers={
            "Content-Disposition": f'attachment; filename="trace-{trace_id}.jsonl"',
        },
    )
