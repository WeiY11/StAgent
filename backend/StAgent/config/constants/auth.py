"""JWT and authentication constants."""

from __future__ import annotations

import uuid

# ---------------------------------------------------------------------------
# JWT / Auth
# ---------------------------------------------------------------------------

JWT_ALGORITHM = "HS256"

# Default local/desktop user identity (passthrough auth).
LOCAL_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
JWT_ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24  # 24 hours
JWT_REFRESH_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7  # 7 days
JWT_ISSUER = "StAgent"

# ---------------------------------------------------------------------------
# RBAC roles
# ---------------------------------------------------------------------------

RBAC_ROLES = {
    "admin": {
        "description": "Full system access",
        "permissions": ["*"],
    },
    "manager": {
        "description": "Department-level management",
        "permissions": [
            "workflow:read", "workflow:execute",
            "task:read", "task:create", "task:cancel",
            "document:read", "document:upload",
            "rule:read",
            "user:read",
            "report:read", "report:create",
        ],
    },
    "operator": {
        "description": "Standard operational access",
        "permissions": [
            "workflow:read", "workflow:execute",
            "task:read", "task:create",
            "document:read", "document:upload",
            "rule:read",
            "report:read",
        ],
    },
    "viewer": {
        "description": "Read-only access",
        "permissions": [
            "workflow:read",
            "task:read",
            "document:read",
            "rule:read",
            "report:read",
        ],
    },
}
