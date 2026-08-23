"""Process-local LRU cache for :func:`StAgent.skills.bundle_payload.build_bundle_payload` results.

Callers (e.g. ``SkillTool``) build a content-revision key; see that module. Not used for
:mod:`StAgent.skills.bundled` (builtin skill directories) or :mod:`StAgent.skills.referenced_bundle`
(@skill prompt injection — uncached per turn).
"""

from __future__ import annotations

import hashlib
from copy import deepcopy
from typing import Any

from StAgent.services.cache.lru import BoundedLRU
from StAgent.services.cache.observability import record_cache_event, record_cache_set

_MAX_ENTRIES = 64
_bundle_cache: BoundedLRU = BoundedLRU(_MAX_ENTRIES)


def _key_digest(key: tuple[Any, ...]) -> str:
    """Hash a tuple cache key to a stable string usable by :class:`BoundedLRU`."""
    return hashlib.sha256(repr(key).encode("utf-8")).hexdigest()


def clear_bundle_payload_cache() -> None:
    """Testing hook."""
    _bundle_cache.clear()


def get_cached_bundle(cache_key: tuple[Any, ...]) -> tuple[str, dict[str, Any]] | None:
    value, hit = _bundle_cache.get(_key_digest(cache_key))
    record_cache_event("skill_bundle", hit=hit, entries=_bundle_cache.size)
    if not hit:
        return None
    body_out, extra = value
    return deepcopy(body_out), deepcopy(extra)


def put_cached_bundle(cache_key: tuple[Any, ...], value: tuple[str, dict[str, Any]]) -> None:
    body_out, extra = value
    evicted = _bundle_cache.set(_key_digest(cache_key), (deepcopy(body_out), deepcopy(extra)))
    record_cache_set("skill_bundle", evicted=evicted)
