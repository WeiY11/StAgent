"""Flow-level access policy shared by workflow HTTP routes."""

from __future__ import annotations

import json
from typing import Any, Literal

from fastapi import HTTPException, status

from leagent.db.models import Flow
from leagent.db.sqlite_compat import same_user_id
from leagent.services.auth.deps import UserPrincipal

FlowAction = Literal["view", "edit", "run", "delete"]

_FIELD_BY_ACTION: dict[FlowAction, str] = {
    "view": "canView",
    "edit": "canEdit",
    "run": "canRun",
    "delete": "canEdit",
}

_PUBLIC_ALIASES = {"all", "anyone", "authenticated", "everyone", "public", "users"}
_ADMIN_ALIASES = {"admin", "admins", "administrator", "administrators"}
_OWNER_ALIASES = {"owner", "owners", "self"}


def _decode_settings(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _permission_tokens(flow: Flow, action: FlowAction) -> set[str] | None:
    settings = _decode_settings(flow.settings)
    permissions = settings.get("permissions")
    if not isinstance(permissions, dict):
        return None

    raw = permissions.get(_FIELD_BY_ACTION[action])
    if raw is None:
        return None
    values = raw if isinstance(raw, list) else [raw]
    tokens = {str(v).strip().lower() for v in values if str(v).strip()}
    return tokens or None


def _is_owner(flow: Flow, principal: UserPrincipal) -> bool:
    return flow.user_id is not None and same_user_id(flow.user_id, principal.user_id)


def _is_admin(principal: UserPrincipal) -> bool:
    return principal.is_superuser or "admin" in principal.roles or "*" in principal.permissions


def can_access_flow(flow: Flow, principal: UserPrincipal, action: FlowAction) -> bool:
    """Evaluate the persisted flow ACL for one action.

    Compatibility defaults:
    - owner and admin can always view/edit/run/delete;
    - public flows without explicit settings can be viewed and run by users;
    - private flows without explicit settings stay owner/admin-only.
    """
    if _is_owner(flow, principal) or _is_admin(principal):
        return True
    if action == "delete":
        return False

    tokens = _permission_tokens(flow, action)
    if tokens is None:
        return bool(flow.is_public and action in {"view", "run"})

    if tokens & _PUBLIC_ALIASES:
        return True
    if tokens & _ADMIN_ALIASES:
        return _is_admin(principal)
    if tokens & _OWNER_ALIASES:
        return _is_owner(flow, principal)
    return False


def require_flow_access(flow: Flow, principal: UserPrincipal, action: FlowAction) -> None:
    if can_access_flow(flow, principal, action):
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=f"Access denied to this flow ({action})",
    )
