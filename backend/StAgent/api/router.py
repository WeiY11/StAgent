"""Central API router that aggregates all versioned sub-routers.

Every router is registered eagerly here (no deferred post-startup mounting).
The mountable surface is declared as data in :mod:`StAgent.api.manifest`; each
entry is imported lazily via :func:`importlib.import_module`. A **required**
router that fails to import raises a ``RuntimeError`` at startup (a missing
core route is a production outage, not a warning). Heavy runtime
*initialization* still happens in the app lifespan — only route registration is
eager.
"""

from __future__ import annotations

from fastapi import APIRouter

from StAgent.api.manifest import CORE_ROUTERS, OPTIONAL_ROUTERS, mount_router

api_router = APIRouter()

v1_router = APIRouter(prefix="/v1")

for _spec in (*CORE_ROUTERS, *OPTIONAL_ROUTERS):
    mount_router(v1_router, _spec)

# ---------------------------------------------------------------------------
# Mount versioned routers. There is no ``v2`` surface — it was nominal (empty)
# so it is intentionally not mounted (see the API-layer review §2.4).
# ---------------------------------------------------------------------------

api_router.include_router(v1_router)
