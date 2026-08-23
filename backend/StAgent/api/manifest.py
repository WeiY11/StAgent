"""Declarative API route manifest with fail-fast mounting.

The central router (:mod:`StAgent.api.router`) is assembled from a static list
of :class:`RouterSpec` entries. Splitting the *declaration* (this manifest) from
the *mounting* (:func:`mount_router`) lets tests reason about the surface and,
critically, lets a broken **required** router fail loudly at startup instead of
being logged-and-skipped — a silently-missing ``/auth`` or ``/chat`` router is a
production outage, not a warning.
"""

from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import APIRouter

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RouterSpec:
    """One mountable router.

    Attributes:
        import_path: Dotted module path exposing ``attr``.
        prefix: URL prefix to mount under (relative to the parent router).
        tags: OpenAPI tags applied to every operation in the router.
        attr: Attribute name of the ``APIRouter`` inside the module.
        required: When ``True`` (default) an import failure raises; when
            ``False`` the failure is logged and the router is skipped.
    """

    import_path: str
    prefix: str = ""
    tags: tuple[str, ...] = ()
    attr: str = "router"
    required: bool = True


def mount_router(parent: APIRouter, spec: RouterSpec) -> None:
    """Import *spec* and mount its router onto *parent*.

    Raises:
        RuntimeError: If a ``required`` router fails to import.
    """
    try:
        module = importlib.import_module(spec.import_path)
        router = getattr(module, spec.attr)
    except Exception as exc:  # noqa: BLE001 - re-raised below for required routers
        if spec.required:
            raise RuntimeError(
                f"required API router failed to load: {spec.import_path}"
            ) from exc
        logger.warning(
            "optional_router_not_mounted", extra={"router": spec.import_path}, exc_info=True
        )
        return
    kwargs: dict[str, object] = {}
    if spec.prefix:
        kwargs["prefix"] = spec.prefix
    if spec.tags:
        kwargs["tags"] = list(spec.tags)
    parent.include_router(router, **kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Core routers — every one is required. A failure to import any of these is a
# hard startup error, not a degraded-mode warning.
# ---------------------------------------------------------------------------

CORE_ROUTERS: tuple[RouterSpec, ...] = (
    RouterSpec("StAgent.api.v1.meta", prefix="/meta", tags=("v1-meta",)),
    RouterSpec("StAgent.api.v1.health", prefix="/health", tags=("v1-health",)),
    RouterSpec("StAgent.api.v1.auth", prefix="/auth", tags=("v1-auth",)),
    RouterSpec(
        "StAgent.api.v1.admin.users",
        prefix="/admin/users",
        tags=("v1-admin-users",),
    ),
    RouterSpec("StAgent.api.v1.chat", prefix="/chat", tags=("v1-chat",)),
    RouterSpec(
        "StAgent.api.v1.chat_projects",
        prefix="/chat-projects",
        tags=("v1-chat-projects",),
    ),
    RouterSpec("StAgent.api.v1.tools", prefix="/tools", tags=("v1-tools",)),
    RouterSpec("StAgent.api.v1.rules", prefix="/rules", tags=("v1-rules",)),
    RouterSpec("StAgent.api.v1.models", prefix="/models", tags=("v1-models",)),
    RouterSpec(
        "StAgent.api.v1.image_gen",
        prefix="/models/image-gen",
        tags=("v1-image-gen",),
    ),
    RouterSpec("StAgent.api.v1.canvas", prefix="/canvas", tags=("v1-canvas",)),
    RouterSpec("StAgent.api.v1.tasks", prefix="/tasks", tags=("v1-tasks",)),
    RouterSpec("StAgent.api.v1.missions", prefix="/missions", tags=("v1-missions",)),
    RouterSpec(
        "StAgent.api.v1.complaints",
        prefix="/complaints",
        tags=("v1-complaints",),
    ),
    RouterSpec("StAgent.workflow.server", tags=("v1-workflow",)),
    RouterSpec("StAgent.api.v1.documents", prefix="/documents", tags=("v1-documents",)),
    RouterSpec("StAgent.api.v1.knowledge", prefix="/knowledge", tags=("v1-knowledge",)),
    RouterSpec("StAgent.api.v1.knowledge_semantic", prefix="/knowledge/semantic", tags=("v1-knowledge-semantic",)),
    RouterSpec(
        "StAgent.api.v1.admin.tasks",
        prefix="/admin/tasks",
        tags=("v1-admin-tasks",),
    ),
    RouterSpec("StAgent.api.v1.files", prefix="/files", tags=("v1-files",)),
    RouterSpec("StAgent.api.v1.pdf_research", prefix="/pdf", tags=("v1-pdf",)),
    RouterSpec("StAgent.api.v1.streams", prefix="/streams", tags=("v1-streams",)),
    RouterSpec("StAgent.api.v1.folders", prefix="/folders", tags=("v1-folders",)),
    RouterSpec(
        "StAgent.api.v1.coding_projects",
        prefix="/coding-projects",
        tags=("v1-coding-projects",),
    ),
    RouterSpec("StAgent.api.v1.pet_space", prefix="/pet-space", tags=("v1-pet-space",)),
    RouterSpec(
        "StAgent.api.v1.folder_items",
        prefix="/folder-items",
        tags=("v1-folder-items",),
    ),
    RouterSpec("StAgent.api.v1.mcp", prefix="/mcp", tags=("v1-mcp",)),
    RouterSpec("StAgent.api.v1.extensions", prefix="/extensions", tags=("v1-extensions",)),
    RouterSpec(
        "StAgent.api.v1.python_env",
        prefix="/python-env",
        tags=("v1-python-env",),
    ),
    RouterSpec(
        "StAgent.api.v1.settings_tokens",
        prefix="/settings",
        tags=("v1-settings-tokens",),
    ),
    RouterSpec(
        "StAgent.api.v1.settings_mail",
        prefix="/settings",
        tags=("v1-settings-mail",),
    ),
    RouterSpec("StAgent.api.v1.skills", prefix="/skills", tags=("v1-skills",)),
    RouterSpec("StAgent.api.v1.channels", prefix="/channels", tags=("v1-channels",)),
    RouterSpec("StAgent.api.v1.templates", prefix="/templates", tags=("v1-templates",)),
    RouterSpec(
        "StAgent.api.v1.workflow_assets",
        prefix="/workflow",
        tags=("v1-workflow-assets",),
    ),
    RouterSpec("StAgent.api.v1.cron", prefix="/cron", tags=("v1-cron",)),
    RouterSpec("StAgent.api.v1.webhooks", prefix="/webhooks", tags=("v1-webhooks",)),
    RouterSpec("StAgent.api.v1.metrics", prefix="/metrics", tags=("v1-metrics",)),
    RouterSpec("StAgent.api.v1.stats", prefix="/stats", tags=("v1-stats",)),
    RouterSpec("StAgent.api.v1.traces", prefix="/traces", tags=("v1-traces",)),
    RouterSpec("StAgent.api.v1.activities", prefix="/activities", tags=("v1-activities",)),
)

# Optional routers: non-critical routes that degrade gracefully.
OPTIONAL_ROUTERS: tuple[RouterSpec, ...] = (
    RouterSpec(
        "StAgent.api.v1.harness",
        prefix="/harness",
        tags=("v1-harness",),
        required=False,
    ),
)
