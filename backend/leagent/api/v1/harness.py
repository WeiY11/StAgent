"""Harness API — query task episode reports and session scores."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from leagent.services.auth.deps import get_current_user_id

router = APIRouter(tags=["harness"])


def _get_report_service() -> Any:
    """Lazy-load DurableReport to avoid import-time config access."""
    from leagent.harness.report import DurableReport

    return DurableReport()


@router.get("/reports")
async def list_reports(
    session_id: str | None = Query(None, description="Filter by session ID"),
    limit: int = Query(50, ge=1, le=200),
    user_id: str = Depends(get_current_user_id),
) -> dict[str, Any]:
    """List harness reports, optionally filtered by session."""
    report_svc = _get_report_service()
    reports = report_svc.list_reports(session_id=session_id, limit=limit)
    return {"reports": reports, "total": len(reports)}


@router.get("/reports/{report_id}")
async def get_report(
    report_id: str,
    user_id: str = Depends(get_current_user_id),
) -> dict[str, Any]:
    """Get a specific harness report by ID."""
    report_svc = _get_report_service()
    report = report_svc.get_report(report_id)
    if report is None:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="Report not found")
    return report


@router.get("/episodes/active")
async def list_active_episodes(
    user_id: str = Depends(get_current_user_id),
) -> dict[str, Any]:
    """List currently active task episodes (in-flight)."""
    # Try to access the process-wide HarnessHook instance
    try:
        from leagent.services import get_service_manager

        sm = get_service_manager()
        hook_manager = sm.runtime_context.hook_manager
        episodes = {}
        for hook in hook_manager._hooks:
            if hasattr(hook, "episodes"):
                episodes = {
                    k: v.model_dump() for k, v in hook.episodes.items()
                }
                break
        return {"active_episodes": episodes}
    except Exception:
        return {"active_episodes": {}}


@router.get("/stats")
async def harness_stats(
    user_id: str = Depends(get_current_user_id),
) -> dict[str, Any]:
    """Aggregated harness statistics."""
    report_svc = _get_report_service()
    reports = report_svc.list_reports(limit=100)

    total = len(reports)
    passed = sum(1 for r in reports if r.get("outcome") == "pass")
    failed = sum(1 for r in reports if r.get("outcome") == "fail")
    partial = sum(1 for r in reports if r.get("outcome") == "partial")

    avg_score = 0.0
    scores = [
        r["score"]["overall"]
        for r in reports
        if r.get("score") and r["score"].get("overall") is not None
    ]
    if scores:
        avg_score = sum(scores) / len(scores)

    total_findings = sum(len(r.get("findings", [])) for r in reports)

    return {
        "total_episodes": total,
        "passed": passed,
        "failed": failed,
        "partial": partial,
        "avg_score": round(avg_score, 4),
        "total_findings": total_findings,
        "pass_rate": round(passed / total, 4) if total > 0 else 0.0,
    }
