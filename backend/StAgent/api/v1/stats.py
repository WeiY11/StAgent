"""Statistics and analytics API endpoints.

Aggregates data from both the ``Task`` and ``WorkflowExecution`` tables so the
dashboard reflects all activity – background tasks **and** workflow runs.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import case
from sqlmodel import func, select

from StAgent.db import DatabaseService, get_database_service
from StAgent.db.models import AgentTrace, Flow, Task, TaskStatus
from StAgent.db.models.workflow_execution import WorkflowExecution
from StAgent.db.sqlite_compat import uuid_column_equals
from StAgent.services.auth import AdminUserId, CurrentUserId

if TYPE_CHECKING:
    from uuid import UUID

router = APIRouter()


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class HomeStats(BaseModel):
    totalFlows: int
    runningTasks: int
    completedToday: int
    successRate: float


class DashboardStats(BaseModel):
    tasksToday: int
    tasksChange: float
    successRate: float
    successRateChange: float
    failedTasks: int
    failedChange: float
    avgDuration: str
    durationChange: float


class UsageDataPoint(BaseModel):
    label: str
    success: int
    failed: int
    date: str


class CacheEntryStat(BaseModel):
    name: str
    hits: int
    misses: int
    sets: int
    evictions: int
    #: TTL-driven drops, split from capacity (LRU) ``evictions`` so operators
    #: can tell "raise max_items" apart from "raise TTL".
    expirations: int = 0
    #: Writes refused (e.g. oversized value) — a bounded-cache pressure signal.
    rejections: int = 0
    #: Single-flight fills and the callers coalesced onto them.
    fills: int = 0
    coalesced: int = 0
    total_requests: int
    hit_rate: float
    #: Hit rate over the recent sliding window; surfaces regressions that the
    #: since-boot cumulative ``hit_rate`` would otherwise mask.
    recent_hit_rate: float = 0.0
    recent_requests: int = 0
    size: int | None = None
    #: Lazy per-cache diagnostics (backend, L2 circuit state, split counters).
    detail: dict | None = None


class CacheStatsResponse(BaseModel):
    """Real-time hit-rate snapshot across every instrumented cache layer."""

    caches: list[CacheEntryStat]
    total_hits: int
    total_misses: int
    total_requests: int
    overall_hit_rate: float
    #: Live :class:`CacheService` configuration (sizes, TTLs) for context.
    config: dict | None = None


class CacheConfigUpdate(BaseModel):
    """Runtime cache-tuning payload for ``POST /stats/cache/config``."""

    max_items: int | None = None


class CostDayPoint(BaseModel):
    """Per-day cost/usage bucket (UTC days)."""

    date: str
    cost_usd: float
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    runs: int
    errors: int


class CostTotals(BaseModel):
    cost_usd: float
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    runs: int
    errors: int


class CostBudgetStatus(BaseModel):
    """Soft monthly budget status (advisory only — never blocks LLM calls)."""

    monthly_budget_usd: float | None = None
    month_to_date_usd: float = 0.0
    #: Spent / budget as a percentage (``0.0`` when no budget is set).
    pct: float = 0.0
    exceeded: bool = False


class CostsResponse(BaseModel):
    days: list[CostDayPoint]
    totals: CostTotals
    budget: CostBudgetStatus


class CostByModelPoint(BaseModel):
    model: str
    runs: int
    cost_usd: float
    avg_latency_ms: float
    total_tokens: int
    success_rate: float


class BudgetStatus(BaseModel):
    monthly_budget_usd: float | None = None


class BudgetUpdate(BaseModel):
    """Soft-budget write payload; ``0``/``null`` clears the budget."""

    monthly_budget_usd: float | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pct_change(current: int | float, previous: int | float) -> float:
    if previous == 0:
        return 0.0
    return round((current - previous) / previous * 100, 1)


def _fmt_duration(ms: float) -> str:
    if ms < 1000:
        return f"{int(ms)}ms"
    if ms < 60_000:
        return f"{ms / 1000:.1f}s"
    return f"{ms / 60_000:.1f}m"


#: ``user_settings`` key holding the soft monthly cost budget (USD).
COST_BUDGET_KEY = "cost_monthly_budget_usd"


async def _load_monthly_budget(db: DatabaseService, user_id: UUID) -> float | None:
    """Read the stored soft monthly budget, or ``None`` when unset/invalid."""
    import json

    raw = await db.repositories.user_settings.get(user_id, COST_BUDGET_KEY)
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    value = float(value)
    return value if value > 0 else None


async def _month_to_date_usd(db: DatabaseService, user_id: UUID) -> float:
    """Sum ``agent_traces.total_cost_usd`` since the start of the current UTC month."""
    now = datetime.utcnow()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    async with db.session() as session:
        res = await session.exec(
            select(func.sum(AgentTrace.total_cost_usd)).where(
                AgentTrace.user_id == str(user_id),
                AgentTrace.started_at >= month_start,
            )
        )
        return float(res.one() or 0.0)


async def _budget_status(db: DatabaseService, user_id: UUID) -> CostBudgetStatus:
    budget = await _load_monthly_budget(db, user_id)
    month_to_date = await _month_to_date_usd(db, user_id)
    pct = round(month_to_date / budget * 100, 1) if budget else 0.0
    return CostBudgetStatus(
        monthly_budget_usd=budget,
        month_to_date_usd=round(month_to_date, 4),
        pct=pct,
        exceeded=bool(budget is not None and month_to_date > budget),
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/home", response_model=HomeStats)
async def get_home_stats(
    user_id: CurrentUserId,
    db: Annotated[DatabaseService, Depends(get_database_service)],
) -> HomeStats:
    """Return summary statistics for the home dashboard."""
    now = datetime.utcnow()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    thirty_days_ago = now - timedelta(days=30)

    async with db.session() as session:
        # Total accessible flows
        total_flows_res = await session.exec(
            select(func.count()).select_from(
                select(Flow)
                .where(
                    (uuid_column_equals(session, Flow.user_id, user_id) | (Flow.is_public == True)),
                    Flow.is_deleted == False,
                )
                .subquery()
            )
        )
        total_flows: int = total_flows_res.one()

        # Currently running tasks
        running_res = await session.exec(
            select(func.count()).select_from(
                select(Task)
                .where(
                    uuid_column_equals(session, Task.user_id, user_id),
                    Task.status == TaskStatus.RUNNING,
                )
                .subquery()
            )
        )
        running_tasks: int = running_res.one()

        # Running workflow executions
        wf_running_res = await session.exec(
            select(func.count()).select_from(
                select(WorkflowExecution)
                .where(
                    uuid_column_equals(session, WorkflowExecution.user_id, user_id),
                    WorkflowExecution.status == "running",
                )
                .subquery()
            )
        )
        running_tasks += wf_running_res.one()

        # Tasks completed today
        completed_today_res = await session.exec(
            select(func.count()).select_from(
                select(Task)
                .where(
                    uuid_column_equals(session, Task.user_id, user_id),
                    Task.status == TaskStatus.COMPLETED,
                    Task.completed_at >= today_start,
                )
                .subquery()
            )
        )
        completed_today: int = completed_today_res.one()

        # Workflow executions completed today
        wf_completed_res = await session.exec(
            select(func.count()).select_from(
                select(WorkflowExecution)
                .where(
                    uuid_column_equals(session, WorkflowExecution.user_id, user_id),
                    WorkflowExecution.status == "completed",
                    WorkflowExecution.completed_at >= today_start,
                )
                .subquery()
            )
        )
        completed_today += wf_completed_res.one()

        # Success rate over last 30 days (tasks)
        terminal_res = await session.exec(
            select(func.count()).select_from(
                select(Task)
                .where(
                    uuid_column_equals(session, Task.user_id, user_id),
                    Task.status.in_([TaskStatus.COMPLETED, TaskStatus.FAILED]),
                    Task.completed_at >= thirty_days_ago,
                )
                .subquery()
            )
        )
        terminal_total: int = terminal_res.one()

        success_count = 0
        if terminal_total > 0:
            success_res = await session.exec(
                select(func.count()).select_from(
                    select(Task)
                    .where(
                        uuid_column_equals(session, Task.user_id, user_id),
                        Task.status == TaskStatus.COMPLETED,
                        Task.completed_at >= thirty_days_ago,
                    )
                    .subquery()
                )
            )
            success_count = success_res.one()

        # Workflow execution terminal counts over 30 days
        wf_terminal_res = await session.exec(
            select(func.count()).select_from(
                select(WorkflowExecution)
                .where(
                    uuid_column_equals(session, WorkflowExecution.user_id, user_id),
                    WorkflowExecution.status.in_(["completed", "failed"]),
                    WorkflowExecution.completed_at >= thirty_days_ago,
                )
                .subquery()
            )
        )
        wf_terminal: int = wf_terminal_res.one()

        wf_success = 0
        if wf_terminal > 0:
            wf_success_res = await session.exec(
                select(func.count()).select_from(
                    select(WorkflowExecution)
                    .where(
                        uuid_column_equals(session, WorkflowExecution.user_id, user_id),
                        WorkflowExecution.status == "completed",
                        WorkflowExecution.completed_at >= thirty_days_ago,
                    )
                    .subquery()
                )
            )
            wf_success = wf_success_res.one()

        all_terminal = terminal_total + wf_terminal
        all_success = success_count + wf_success
        success_rate = round(all_success / all_terminal * 100, 1) if all_terminal else 0.0

    return HomeStats(
        totalFlows=total_flows,
        runningTasks=running_tasks,
        completedToday=completed_today,
        successRate=success_rate,
    )


@router.get("/dashboard", response_model=DashboardStats)
async def get_dashboard_stats(
    user_id: CurrentUserId,
    db: Annotated[DatabaseService, Depends(get_database_service)],
    timeRange: str = Query(default="today", pattern="^(today|week|month)$"),
) -> DashboardStats:
    """Return period-over-period dashboard statistics."""
    now = datetime.utcnow()

    if timeRange == "today":
        period_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        prev_start = period_start - timedelta(days=1)
        prev_end = period_start
    elif timeRange == "week":
        period_start = now - timedelta(days=7)
        prev_start = period_start - timedelta(days=7)
        prev_end = period_start
    else:  # month
        period_start = now - timedelta(days=30)
        prev_start = period_start - timedelta(days=30)
        prev_end = period_start

    async def _count(session, status_filter, start, end=None):
        q = select(Task).where(
            uuid_column_equals(session, Task.user_id, user_id),
            Task.created_at >= start,
        )
        if end:
            q = q.where(Task.created_at < end)
        if status_filter:
            q = q.where(Task.status == status_filter)
        res = await session.exec(select(func.count()).select_from(q.subquery()))
        return res.one()

    async def _wf_count(session, status_filter, start, end=None):
        q = select(WorkflowExecution).where(
            uuid_column_equals(session, WorkflowExecution.user_id, user_id),
            WorkflowExecution.created_at >= start,
        )
        if end:
            q = q.where(WorkflowExecution.created_at < end)
        if status_filter:
            q = q.where(WorkflowExecution.status == status_filter)
        res = await session.exec(select(func.count()).select_from(q.subquery()))
        return res.one()

    async def _avg_duration(session, start, end=None):
        q = select(func.avg(Task.duration_ms)).where(
            uuid_column_equals(session, Task.user_id, user_id),
            Task.status == TaskStatus.COMPLETED,
            Task.duration_ms.isnot(None),
            Task.created_at >= start,
        )
        if end:
            q = q.where(Task.created_at < end)
        res = await session.exec(q)
        val = res.one()
        return float(val) if val else 0.0

    async def _wf_avg_duration(session, start, end=None):
        q = select(func.avg(WorkflowExecution.duration_ms)).where(
            uuid_column_equals(session, WorkflowExecution.user_id, user_id),
            WorkflowExecution.status == "completed",
            WorkflowExecution.duration_ms > 0,
            WorkflowExecution.created_at >= start,
        )
        if end:
            q = q.where(WorkflowExecution.created_at < end)
        res = await session.exec(q)
        val = res.one()
        return float(val) if val else 0.0

    async with db.session() as session:
        tasks_cur = await _count(session, None, period_start)
        tasks_prev = await _count(session, None, prev_start, prev_end)
        wf_tasks_cur = await _wf_count(session, None, period_start)
        wf_tasks_prev = await _wf_count(session, None, prev_start, prev_end)

        failed_cur = await _count(session, TaskStatus.FAILED, period_start)
        failed_prev = await _count(session, TaskStatus.FAILED, prev_start, prev_end)
        wf_failed_cur = await _wf_count(session, "failed", period_start)
        wf_failed_prev = await _wf_count(session, "failed", prev_start, prev_end)

        success_cur = await _count(session, TaskStatus.COMPLETED, period_start)
        success_prev = await _count(session, TaskStatus.COMPLETED, prev_start, prev_end)
        wf_success_cur = await _wf_count(session, "completed", period_start)
        wf_success_prev = await _wf_count(session, "completed", prev_start, prev_end)

        dur_cur = await _avg_duration(session, period_start)
        dur_prev = await _avg_duration(session, prev_start, prev_end)
        wf_dur_cur = await _wf_avg_duration(session, period_start)
        wf_dur_prev = await _wf_avg_duration(session, prev_start, prev_end)

    all_tasks_cur = tasks_cur + wf_tasks_cur
    all_tasks_prev = tasks_prev + wf_tasks_prev

    all_failed_cur = failed_cur + wf_failed_cur
    all_failed_prev = failed_prev + wf_failed_prev

    all_success_cur = success_cur + wf_success_cur
    all_success_prev = success_prev + wf_success_prev

    terminal_cur = all_success_cur + all_failed_cur
    rate_cur = round(all_success_cur / terminal_cur * 100, 1) if terminal_cur else 0.0

    terminal_prev = all_success_prev + all_failed_prev
    rate_prev = round(all_success_prev / terminal_prev * 100, 1) if terminal_prev else 0.0

    # Weighted average duration across both sources
    dur_total_cur = tasks_cur + wf_tasks_cur
    if dur_total_cur > 0:
        combined_dur_cur = (dur_cur * tasks_cur + wf_dur_cur * wf_tasks_cur) / dur_total_cur
    else:
        combined_dur_cur = 0.0

    dur_total_prev = tasks_prev + wf_tasks_prev
    if dur_total_prev > 0:
        combined_dur_prev = (dur_prev * tasks_prev + wf_dur_prev * wf_tasks_prev) / dur_total_prev
    else:
        combined_dur_prev = 0.0

    return DashboardStats(
        tasksToday=all_tasks_cur,
        tasksChange=_pct_change(all_tasks_cur, all_tasks_prev),
        successRate=rate_cur,
        successRateChange=_pct_change(rate_cur, rate_prev),
        failedTasks=all_failed_cur,
        failedChange=_pct_change(all_failed_cur, all_failed_prev),
        avgDuration=_fmt_duration(combined_dur_cur),
        durationChange=_pct_change(combined_dur_cur, combined_dur_prev),
    )


@router.get("/usage", response_model=list[UsageDataPoint])
async def get_usage_data(
    user_id: CurrentUserId,
    db: Annotated[DatabaseService, Depends(get_database_service)],
    timeRange: str = Query(default="week", pattern="^(today|week|month)$"),
) -> list[UsageDataPoint]:
    """Return time-series task usage data for charts."""
    now = datetime.utcnow()

    if timeRange == "today":
        # Hourly buckets for today
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        buckets = [(day_start + timedelta(hours=h), day_start + timedelta(hours=h + 1)) for h in range(now.hour + 1)]
        label_fmt = "%H:00"
    elif timeRange == "week":
        # Daily buckets for last 7 days
        buckets = [(now - timedelta(days=d + 1), now - timedelta(days=d)) for d in reversed(range(7))]
        label_fmt = "%a"
    else:  # month
        # Daily buckets for last 30 days
        buckets = [(now - timedelta(days=d + 1), now - timedelta(days=d)) for d in reversed(range(30))]
        label_fmt = "%m/%d"

    async with db.session() as session:
        result = []
        for bucket_start, bucket_end in buckets:

            def _make_count_query(status, start: datetime, end: datetime):
                return select(func.count()).select_from(
                    select(Task)
                    .where(
                        uuid_column_equals(session, Task.user_id, user_id),
                        Task.status == status,
                        Task.created_at >= start,
                        Task.created_at < end,
                    )
                    .subquery()
                )

            def _make_wf_count_query(status_str: str, start: datetime, end: datetime):
                return select(func.count()).select_from(
                    select(WorkflowExecution)
                    .where(
                        uuid_column_equals(
                            session,
                            WorkflowExecution.user_id,
                            user_id,
                        ),
                        WorkflowExecution.status == status_str,
                        WorkflowExecution.created_at >= start,
                        WorkflowExecution.created_at < end,
                    )
                    .subquery()
                )

            s_res = await session.exec(_make_count_query(TaskStatus.COMPLETED, bucket_start, bucket_end))
            f_res = await session.exec(_make_count_query(TaskStatus.FAILED, bucket_start, bucket_end))
            wf_s_res = await session.exec(_make_wf_count_query("completed", bucket_start, bucket_end))
            wf_f_res = await session.exec(_make_wf_count_query("failed", bucket_start, bucket_end))

            result.append(
                UsageDataPoint(
                    label=bucket_start.strftime(label_fmt),
                    success=s_res.one() + wf_s_res.one(),
                    failed=f_res.one() + wf_f_res.one(),
                    date=bucket_start.date().isoformat(),
                )
            )

    return result


@router.get("/cache", response_model=CacheStatsResponse)
async def get_cache_stats(user_id: CurrentUserId) -> CacheStatsResponse:
    """Return a real-time hit-rate snapshot for all instrumented caches.

    Aggregates the process-wide :class:`CacheStatsRegistry`, which every cache
    layer (``CacheService`` in-memory LRU, workflow node-output cache, the
    ``cache_manager`` tool cache, the LLM tool-schema cache, and the tiered
    agent-memory cache) reports into via ``record_cache_event``.
    """
    from StAgent.services.cache.observability import get_cache_stats_registry

    snapshot = get_cache_stats_registry().snapshot()

    config: dict | None = None
    try:
        from StAgent.services.cache.service import get_cache_service

        config = get_cache_service().config_snapshot()
    except Exception:  # noqa: BLE001 - config context is best-effort
        config = None

    return CacheStatsResponse(
        caches=[CacheEntryStat(**c) for c in snapshot["caches"]],
        total_hits=snapshot["total_hits"],
        total_misses=snapshot["total_misses"],
        total_requests=snapshot["total_requests"],
        overall_hit_rate=snapshot["overall_hit_rate"],
        config=config,
    )


@router.post("/cache/config", response_model=CacheStatsResponse)
async def update_cache_config(
    _admin_id: AdminUserId,
    payload: CacheConfigUpdate,
) -> CacheStatsResponse:
    """Apply a runtime cache-tuning change and return the fresh snapshot.

    Currently exposes the in-memory :class:`CacheService` capacity bound
    (``max_items``); shrinking evicts LRU entries immediately. Wired as the
    operator/autotuning lever behind **Admin → Cache** (admin-only).
    """
    from fastapi import HTTPException

    from StAgent.services.cache.observability import get_cache_stats_registry
    from StAgent.services.cache.service import get_cache_service

    try:
        service = get_cache_service()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail="CacheService not initialized") from exc

    try:
        config = service.update_limits(max_items=payload.max_items)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    snapshot = get_cache_stats_registry().snapshot()
    return CacheStatsResponse(
        caches=[CacheEntryStat(**c) for c in snapshot["caches"]],
        total_hits=snapshot["total_hits"],
        total_misses=snapshot["total_misses"],
        total_requests=snapshot["total_requests"],
        overall_hit_rate=snapshot["overall_hit_rate"],
        config=config,
    )


@router.get("/costs", response_model=CostsResponse)
async def get_cost_stats(
    user_id: CurrentUserId,
    db: Annotated[DatabaseService, Depends(get_database_service)],
    days: int = Query(default=30, ge=1, le=90),
) -> CostsResponse:
    """Return daily cost/usage buckets from ``agent_traces`` plus the soft budget status."""
    now = datetime.utcnow()
    since = now - timedelta(days=days)

    day_expr = func.date(AgentTrace.started_at).label("day")
    error_case = case((AgentTrace.error.is_not(None), 1), else_=0)  # type: ignore[union-attr]

    async with db.session() as session:
        res = await session.exec(
            select(  # type: ignore[call-overload]
                day_expr,
                func.sum(AgentTrace.total_cost_usd),
                func.sum(AgentTrace.input_tokens),
                func.sum(AgentTrace.output_tokens),
                func.sum(AgentTrace.cache_read_tokens),
                func.count(),
                func.sum(error_case),
            )
            .where(
                AgentTrace.user_id == str(user_id),
                AgentTrace.started_at >= since,
            )
            .group_by(day_expr)
            .order_by(day_expr)
        )
        grouped = {str(row[0]): row for row in res.all()}

    points: list[CostDayPoint] = []
    for offset in reversed(range(days)):
        day = (now - timedelta(days=offset)).date()
        row = grouped.get(day.isoformat()) or (None, None, None, None, None, None, None)
        points.append(
            CostDayPoint(
                date=day.isoformat(),
                cost_usd=round(float(row[1] or 0.0), 4),
                input_tokens=int(row[2] or 0),
                output_tokens=int(row[3] or 0),
                cache_read_tokens=int(row[4] or 0),
                runs=int(row[5] or 0),
                errors=int(row[6] or 0),
            )
        )

    totals = CostTotals(
        cost_usd=round(sum(p.cost_usd for p in points), 4),
        input_tokens=sum(p.input_tokens for p in points),
        output_tokens=sum(p.output_tokens for p in points),
        cache_read_tokens=sum(p.cache_read_tokens for p in points),
        runs=sum(p.runs for p in points),
        errors=sum(p.errors for p in points),
    )
    budget = await _budget_status(db, user_id)
    return CostsResponse(days=points, totals=totals, budget=budget)


@router.get("/costs/by-model", response_model=list[CostByModelPoint])
async def get_cost_stats_by_model(
    user_id: CurrentUserId,
    days: int = Query(default=30, ge=1, le=365),
) -> list[CostByModelPoint]:
    """Return per-model cost/latency/success-rate aggregates for the current user."""
    from StAgent.telemetry.trace.store import get_trace_store

    rows = await get_trace_store().stats_by_model(days=days, user_id=str(user_id))
    return [
        CostByModelPoint(
            model=row["model"],
            runs=row["runs"],
            cost_usd=round(float(row["total_cost_usd"] or 0.0), 4),
            avg_latency_ms=round(float(row["avg_latency_ms"] or 0.0), 1),
            total_tokens=int(row["total_input_tokens"] or 0) + int(row["total_output_tokens"] or 0),
            success_rate=round(float(row["success_rate"] or 0.0) * 100, 1),
        )
        for row in rows
    ]


@router.get("/costs/budget", response_model=BudgetStatus)
async def get_cost_budget(
    user_id: CurrentUserId,
    db: Annotated[DatabaseService, Depends(get_database_service)],
) -> BudgetStatus:
    """Return the stored soft monthly cost budget (``null`` when unset)."""
    return BudgetStatus(monthly_budget_usd=await _load_monthly_budget(db, user_id))


@router.put("/costs/budget", response_model=BudgetStatus)
async def put_cost_budget(
    user_id: CurrentUserId,
    db: Annotated[DatabaseService, Depends(get_database_service)],
    payload: BudgetUpdate,
) -> BudgetStatus:
    """Upsert (``>0``) or clear (``0``/``null``) the soft monthly cost budget."""
    import json

    value = payload.monthly_budget_usd
    if value is not None:
        if value < 0:
            raise HTTPException(status_code=422, detail="monthly_budget_usd must be >= 0")
        value = value or None
    await db.repositories.user_settings.set(
        user_id, COST_BUDGET_KEY, json.dumps(value) if value is not None else None
    )
    return BudgetStatus(monthly_budget_usd=value)
