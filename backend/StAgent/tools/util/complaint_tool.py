"""Complaint Tool — agent-facing customer complaint ticket management."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import structlog

from StAgent.db.models.base import utc_now
from StAgent.db.models.complaint import (
    COMPLAINT_CATEGORIES,
    COMPLAINT_SEVERITIES,
    COMPLAINT_STATUSES,
    Complaint,
)
from StAgent.db.sqlite_compat import same_user_id
from StAgent.tools.base import BaseTool, ToolCategory, ToolContext

logger = structlog.get_logger(__name__)


def _complaint_to_dict(complaint: Complaint) -> dict[str, Any]:
    return {
        "id": str(complaint.id),
        "title": complaint.title,
        "description": complaint.description,
        "customer_name": complaint.customer_name,
        "customer_contact": complaint.customer_contact,
        "category": complaint.category,
        "severity": complaint.severity,
        "status": complaint.status,
        "source": complaint.source,
        "resolution": complaint.resolution,
        "resolved_at": complaint.resolved_at.isoformat() if complaint.resolved_at else None,
        "created_at": complaint.created_at.isoformat(),
        "updated_at": complaint.updated_at.isoformat(),
    }


class ComplaintTool(BaseTool):
    """Create, query and update customer complaint tickets.

    All operations go through the database Complaint model so changes are
    immediately visible in the REST API and the frontend Complaints page.
    """

    name = "complaint"
    description = (
        "Manage customer complaint tickets. Use this whenever the user reports "
        "a customer complaint, asks to log/register a complaint, wants to list "
        "or look up existing complaints, or wants to update a complaint's "
        "progress (start handling, resolve with a resolution note, close). "
        "Draft customer-facing replies yourself in chat; this tool only "
        "persists ticket data."
    )
    category = ToolCategory.UTIL
    version = "1.0.0"
    timeout_sec = 30
    aliases = ["complaints", "complaint_ticket"]
    search_hint = "complaint customer ticket create list update resolve close 客诉 投诉 工单"
    is_concurrency_safe = False
    is_read_only = False
    interrupt_behavior = "cancel"
    max_result_size_chars = 50_000

    def get_activity_description(self, params: dict[str, Any] | None = None) -> str | None:
        op = (params or {}).get("operation", "list_complaints")
        return f"Complaint operation ({op})"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": [
                        "create_complaint",
                        "list_complaints",
                        "get_complaint",
                        "update_complaint",
                    ],
                    "description": (
                        "Complaint operation to perform. create_complaint logs a "
                        "new ticket; list_complaints returns the user's tickets "
                        "(optionally filtered by status/category/severity); "
                        "get_complaint returns one ticket by id; update_complaint "
                        "edits fields, transitions status, and records the "
                        "resolution."
                    ),
                },
                "complaint_id": {
                    "type": "string",
                    "description": "Complaint UUID (for get/update).",
                },
                "title": {
                    "type": "string",
                    "description": "Short complaint summary (for create/update).",
                },
                "description": {
                    "type": "string",
                    "description": "Detailed complaint description (for create/update).",
                },
                "customer_name": {
                    "type": "string",
                    "description": "Name of the complaining customer.",
                },
                "customer_contact": {
                    "type": "string",
                    "description": "Customer contact (phone, email, order id, ...).",
                },
                "category": {
                    "type": "string",
                    "enum": list(COMPLAINT_CATEGORIES),
                    "description": "Complaint category.",
                },
                "severity": {
                    "type": "string",
                    "enum": list(COMPLAINT_SEVERITIES),
                    "description": "Complaint severity.",
                },
                "status": {
                    "type": "string",
                    "enum": list(COMPLAINT_STATUSES),
                    "description": "New status (for update_complaint / list filter).",
                },
                "resolution": {
                    "type": "string",
                    "description": "Resolution note (for update_complaint when resolving).",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max tickets to return for list_complaints (default 20).",
                },
            },
            "required": ["operation"],
            "additionalProperties": False,
        }

    async def execute(self, params: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        from StAgent.services.service_manager import get_service_manager

        sm = get_service_manager()
        if sm.db is None:
            return {"error": "Database service unavailable"}

        operation = params["operation"]
        dispatch = {
            "create_complaint": self._create,
            "list_complaints": self._list,
            "get_complaint": self._get,
            "update_complaint": self._update,
        }

        handler = dispatch.get(operation)
        if handler is None:
            return {"error": f"Unknown operation: {operation}"}

        try:
            return await handler(sm, params, context)
        except Exception as exc:
            logger.error("complaint_operation_failed", operation=operation, error=str(exc))
            return {"error": str(exc)}

    def _user_id(self, ctx: ToolContext) -> UUID | None:
        return UUID(ctx.user_id) if ctx.user_id else None

    async def _create(self, sm: Any, params: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        title = params.get("title")
        if not title:
            return {"error": "'title' is required for create_complaint"}

        complaint = Complaint(
            title=title,
            description=params.get("description"),
            customer_name=params.get("customer_name"),
            customer_contact=params.get("customer_contact"),
            category=params.get("category") or "other",
            severity=params.get("severity") or "medium",
            source="chat",
            session_id=ctx.session_id,
            user_id=self._user_id(ctx),
        )
        complaint = await sm.db.repositories.complaints.create(complaint)
        return {"created": True, "complaint": _complaint_to_dict(complaint)}

    async def _list(self, sm: Any, params: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        uid = self._user_id(ctx)
        if uid is None:
            return {"error": "No user context available"}

        limit = int(params.get("limit") or 20)
        complaints = await sm.db.repositories.complaints.list_for_user(
            uid,
            status=params.get("status"),
            category=params.get("category"),
            severity=params.get("severity"),
            limit=max(1, min(limit, 100)),
        )
        return {
            "count": len(complaints),
            "complaints": [_complaint_to_dict(c) for c in complaints],
        }

    async def _get_owned(self, sm: Any, params: dict[str, Any], ctx: ToolContext) -> Complaint | dict[str, Any]:
        complaint_id = params.get("complaint_id")
        if not complaint_id:
            return {"error": "'complaint_id' is required"}

        complaint = await sm.db.repositories.complaints.get(UUID(complaint_id))
        if complaint is None or complaint.is_deleted:
            return {"error": "Complaint not found"}
        if not same_user_id(complaint.user_id, self._user_id(ctx)):
            return {"error": "Access denied"}
        return complaint

    async def _get(self, sm: Any, params: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        result = await self._get_owned(sm, params, ctx)
        if isinstance(result, dict):
            return result
        return {"complaint": _complaint_to_dict(result)}

    async def _update(self, sm: Any, params: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        result = await self._get_owned(sm, params, ctx)
        if isinstance(result, dict):
            return result
        complaint = result

        for field_name in (
            "title",
            "description",
            "customer_name",
            "customer_contact",
            "category",
            "severity",
            "status",
            "resolution",
        ):
            if params.get(field_name) is not None:
                setattr(complaint, field_name, params[field_name])

        if params.get("status") == "resolved" and complaint.resolved_at is None:
            complaint.resolved_at = utc_now()

        complaint.updated_at = utc_now()
        complaint = await sm.db.repositories.complaints.update(complaint)
        return {"updated": True, "complaint": _complaint_to_dict(complaint)}
