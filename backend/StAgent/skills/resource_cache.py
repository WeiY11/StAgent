"""Process-local LRU cache for skill resource file reads (read_skill_resource).

Keys include resolved path and mtime so edits invalidate automatically.
"""

from __future__ import annotations

import hashlib
from copy import deepcopy
from pathlib import Path
from typing import Any

from StAgent.services.cache.lru import BoundedLRU
from StAgent.services.cache.observability import record_cache_event, record_cache_set

_CACHE_MAX = 384
_resource_payload_cache: BoundedLRU = BoundedLRU(_CACHE_MAX)


def _key_digest(key: tuple[Any, ...]) -> str:
    """Hash a tuple cache key to a stable string usable by :class:`BoundedLRU`."""
    return hashlib.sha256(repr(key).encode("utf-8")).hexdigest()


def clear_skill_resource_read_cache() -> None:
    """Testing hook — drop all cached resource payloads."""
    _resource_payload_cache.clear()


def get_cached_resource_payload(key: tuple[str, int | float, int]) -> dict[str, Any] | None:
    """Return a deep copy of a cached payload, or None."""
    value, hit = _resource_payload_cache.get(_key_digest(key))
    record_cache_event("skill_resource", hit=hit, entries=_resource_payload_cache.size)
    return deepcopy(value) if hit else None


def put_cached_resource_payload(key: tuple[str, int | float, int], payload: dict[str, Any]) -> None:
    """Store a deep copy so callers cannot mutate the cache."""
    evicted = _resource_payload_cache.set(_key_digest(key), deepcopy(payload))
    record_cache_set("skill_resource", evicted=evicted)


def cache_key_for_path(path: Path, effective_cap: int) -> tuple[str, int | float, int] | None:
    """Build cache key, or None if stat fails (caller should read without cache).

    *effective_cap* must match the byte cap passed to the reader (e.g.
    ``max(1024, min(max_bytes, MAX_CHARS))``).
    """
    try:
        st = path.stat()
    except OSError:
        return None
    resolved = str(path.resolve())
    mkey: int | float
    mtime_ns = getattr(st, "st_mtime_ns", None)
    if mtime_ns is not None:
        mkey = int(mtime_ns)
    else:
        mkey = float(st.st_mtime)
    return (resolved, mkey, effective_cap)
