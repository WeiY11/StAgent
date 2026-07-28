"""``workflow_save`` — persist an agent-authored workflow graph as a Flow row.

Closes the agent production loop: the agent designs a DAG (the same
``flow_data`` JSON shape used by ``chat_workflow_embed_emit`` and the Flow
editor), saves it here to obtain a durable ``flow_id``, then drives it with
``workflow_run`` / ``workflow_status``. The graph is validated against the
canonical workflow document (engine schema) before it is stored — invalid
graphs never reach the database.
"""

from __future__ import annotations

import json
from typing import Any

import structlog
from sqlalchemy import text

from leagent.chat_workflow.workflow_embed import (
    WorkflowEmbedValidationError,
    validate_workflow_embed,
)
from leagent.tools.base import ToolCategory, ToolContext, ToolResult
from leagent.tools.workflow._schema_tool import SchemaWorkflowTool
from leagent.workflow.nodes import get_registry

logger = structlog.get_logger(__name__)


class WorkflowSaveTool(SchemaWorkflowTool):
    name = "workflow_save"
    description = (
        "Persist a workflow graph as a reusable Flow and return its flow_id. flow_data "
        "uses the SAME JSON shape as chat_workflow_embed_emit / the Flow editor (nodes "
        "dict, control.start/end, optional ui) and is validated by the engine before "
        "saving. Drive it with workflow_run, then workflow_status — the "
        "design -> run -> evaluate loop. Save only when a graph should be kept/reused; "
        "for one-off runs prefer chat_workflow_embed_emit."
    )
    category = ToolCategory.WORKFLOW
    is_concurrency_safe = False
    parameters_schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Human-readable workflow name"},
            "flow_data": {
                "type": "object",
                "description": "Canonical workflow document (nodes, control, optional ui).",
            },
            "description": {"type": "string", "description": "Optional description"},
            "flow_id": {
                "type": "string",
                "description": "Optional UUID of an existing Flow to update in place.",
            },
            "publish": {
                "type": "boolean",
                "description": "Save as published (runnable) rather than draft. Default true.",
                "default": True,
            },
        },
        "required": ["name", "flow_data"],
    }

    async def _execute(self, context: ToolContext, **kwargs: Any) -> ToolResult:
        from uuid import UUID

        from leagent.tools.workflow import workflow_error_result

        raw_fd = kwargs.get("flow_data")
        if not isinstance(raw_fd, dict):
            return ToolResult(success=False, error="flow_data must be an object")

        try:
            doc, digest = validate_workflow_embed(raw_fd, node_registry=get_registry())
        except WorkflowEmbedValidationError as exc:
            return ToolResult(success=False, error=f"Invalid workflow graph: {exc}")

        canonical = doc.to_dict()
        ui = raw_fd.get("ui")
        if isinstance(ui, dict):
            canonical["ui"] = ui

        try:
            # TODO: migrate to FlowRepository — this method uses complex SQLite-specific raw SQL
            # (hex ID formatting, parent_id text lookups, raw INSERT/UPDATE) that cannot be easily
            # expressed through the repository pattern. Requires FlowRepository extensions for
            # SQLite-compatible upsert and parent_id text resolution.
            from leagent.config.constants import LOCAL_USER_ID
            from leagent.db.models.base import utc_now
            from leagent.db.models.flow import Flow, FlowStatus, FlowType
            from leagent.db.sqlite_compat import (
                load_entity_by_id,
                same_user_id,
                session_dialect_name,
                sqlite_parent_id_text,
            )
            from leagent.services.service_manager import get_service_manager

            sm = get_service_manager()
            if sm.db is None:
                return ToolResult(success=False, error="Database not available")

            user_id = LOCAL_USER_ID
            if context and getattr(context, "user_id", None):
                user_id = UUID(str(context.user_id))

            name = str(kwargs.get("name") or doc.name or "Untitled Workflow")
            description = str(kwargs.get("description") or doc.description or "")
            status = (
                FlowStatus.PUBLISHED if kwargs.get("publish", True) else FlowStatus.DRAFT
            )
            data_json = json.dumps(canonical)

            existing_id = kwargs.get("flow_id")
            async with sm.db.session() as session:
                flow: Flow | None = None
                sqlite = session_dialect_name(session) == "sqlite"
                if existing_id:
                    flow = await load_entity_by_id(
                        session,
                        Flow,
                        UUID(str(existing_id)),
                        parent_table="flows",
                    )
                    if flow is None:
                        return ToolResult(
                            success=False,
                            error="Flow not found or access denied",
                        )
                if flow is not None:
                    now = utc_now()
                    if flow.is_deleted or (
                        flow.user_id is not None
                        and not same_user_id(flow.user_id, user_id)
                    ):
                        return ToolResult(
                            success=False,
                            error="Flow not found or access denied",
                        )
                    if sqlite:
                        await session.execute(
                            text(
                                """
                                UPDATE flows
                                SET name = :name,
                                    description = :description,
                                    status = :status,
                                    data = :data,
                                    user_id = COALESCE(user_id, :user_id),
                                    updated_at = :updated_at
                                WHERE lower(replace(CAST(id AS TEXT), '-', '')) = :flow_id
                                """
                            ),
                            {
                                "name": name,
                                "description": description,
                                "status": status.value,
                                "data": data_json,
                                "user_id": await sqlite_parent_id_text(
                                    session, "users", user_id
                                ),
                                "updated_at": now,
                                "flow_id": str(flow.id).replace("-", "").lower(),
                            },
                        )
                        session.expunge(flow)
                        flow.name = name
                        flow.description = description
                        flow.status = status
                        flow.data = data_json
                        if flow.user_id is None:
                            flow.user_id = user_id
                        flow.updated_at = now
                    else:
                        flow.name = name
                        flow.description = description
                        flow.status = status
                        flow.data = data_json
                        if flow.user_id is None:
                            flow.user_id = user_id
                        flow.updated_at = now
                        session.add(flow)
                else:
                    flow = Flow(
                        name=name,
                        description=description,
                        status=status,
                        flow_type=FlowType.WORKFLOW,
                        data=data_json,
                        user_id=user_id,
                    )
                    if sqlite:
                        await session.execute(
                            text(
                                """
                                INSERT INTO flows (
                                    is_deleted, deleted_at, created_at, updated_at,
                                    id, name, description, icon, icon_bg_color,
                                    status, flow_type, is_public, tags, data,
                                    version, parent_id, user_id, folder_id,
                                    workspace_id, settings, run_count, last_run_at,
                                    avg_run_time_ms, endpoint_name
                                )
                                VALUES (
                                    :is_deleted, :deleted_at, :created_at, :updated_at,
                                    :id, :name, :description, :icon, :icon_bg_color,
                                    :status, :flow_type, :is_public, :tags, :data,
                                    :version, :parent_id, :user_id, :folder_id,
                                    :workspace_id, :settings, :run_count, :last_run_at,
                                    :avg_run_time_ms, :endpoint_name
                                )
                                """
                            ),
                            {
                                "is_deleted": bool(flow.is_deleted),
                                "deleted_at": flow.deleted_at,
                                "created_at": flow.created_at,
                                "updated_at": flow.updated_at,
                                "id": flow.id.hex,
                                "name": flow.name,
                                "description": flow.description,
                                "icon": flow.icon,
                                "icon_bg_color": flow.icon_bg_color,
                                "status": flow.status.value,
                                "flow_type": flow.flow_type.value,
                                "is_public": bool(flow.is_public),
                                "tags": flow.tags,
                                "data": flow.data,
                                "version": flow.version,
                                "parent_id": (
                                    await sqlite_parent_id_text(
                                        session, "flows", flow.parent_id
                                    )
                                    if flow.parent_id is not None
                                    else None
                                ),
                                "user_id": await sqlite_parent_id_text(
                                    session, "users", user_id
                                ),
                                "folder_id": (
                                    await sqlite_parent_id_text(
                                        session, "folders", flow.folder_id
                                    )
                                    if flow.folder_id is not None
                                    else None
                                ),
                                "workspace_id": (
                                    await sqlite_parent_id_text(
                                        session, "workspaces", flow.workspace_id
                                    )
                                    if flow.workspace_id is not None
                                    else None
                                ),
                                "settings": flow.settings,
                                "run_count": flow.run_count,
                                "last_run_at": flow.last_run_at,
                                "avg_run_time_ms": flow.avg_run_time_ms,
                                "endpoint_name": flow.endpoint_name,
                            },
                        )
                        loaded = await load_entity_by_id(
                            session, Flow, flow.id, parent_table="flows"
                        )
                        if loaded is None:
                            raise RuntimeError("Inserted Flow could not be reloaded")
                        flow = loaded
                    else:
                        session.add(flow)
                if not sqlite:
                    await session.flush()
                    await session.refresh(flow)
                flow_id = str(flow.id)
                node_count = len(canonical.get("nodes", {}) or {})

            logger.info(
                "workflow_saved", flow_id=flow_id, node_count=node_count, digest=digest,
            )
            return ToolResult(
                success=True,
                data={
                    "flow_id": flow_id,
                    "name": name,
                    "digest": digest,
                    "node_count": node_count,
                    "status": status.value,
                },
            )
        except Exception as exc:  # noqa: BLE001
            return workflow_error_result("save", exc)
