"""Scoped cache-key construction.

A logical cache key alone is not safe to share across users, workspaces or
data versions. :func:`build_scoped_cache_key` folds an owner scope, a resource
scope, a monotonic data ``generation`` and a canonicalized variant digest into
one opaque string so entries can never leak across tenants and are invalidated
by bumping the generation rather than scanning prefixes.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping

_KEY_SCHEMA = "v2"


def _digest(value: object | None, *, size: int = 16) -> str:
    raw = "-" if value is None else str(value)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:size]


def build_scoped_cache_key(
    logical_key: str,
    *,
    owner_id: object | None = None,
    resource_id: object | None = None,
    generation: int = 0,
    vary: Mapping[str, Any] | None = None,
) -> str:
    """Return a stable key isolated by owner, resource and data generation."""
    payload = json.dumps(
        dict(vary or {}), sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, default=str,
    )
    return ":".join(
        (
            _KEY_SCHEMA,
            f"o{_digest(owner_id)}",
            f"r{_digest(resource_id)}",
            f"g{max(0, int(generation))}",
            f"k{_digest(logical_key, size=24)}",
            f"x{_digest(payload, size=24)}",
        )
    )
