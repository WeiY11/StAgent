"""Shared bounded-LRU primitives for every in-memory cache layer.

Before this module the codebase carried three near-identical hand-rolled LRUs
(``CacheService`` in-memory store, the ``cache_manager`` tool cache, and the
agent-memory L1). They diverged on thread-safety, eviction accounting, and TTL
handling. :class:`BoundedLRU` is the single, thread-safe, O(1) primitive they
now share; :class:`ShardedLRU` composes several shards to cut lock contention
under concurrent access while presenting the same surface.

Design notes:

* **Thread-safe & sync.** All operations are O(1) and hold a short-lived
  ``threading.Lock``, so the primitive is safe to call from the event loop
  *and* from threadpool executors (sync tools) without ``await``. Async
  wrappers keep their ``async def`` signatures for API compatibility but no
  longer need an ``asyncio.Lock``.
* **Split eviction accounting.** ``lru_evictions`` (capacity pressure) and
  ``expirations`` (TTL) are counted separately so monitoring can tell "raise
  ``max_size``" apart from "raise TTL".
* **No observability import.** This module stays a pure data structure; callers
  translate :meth:`BoundedLRU.stats` / return values into
  ``record_cache_event`` calls. That keeps it trivially unit-testable.
"""

from __future__ import annotations

import fnmatch
import threading
import time
from typing import Any


class _Entry:
    """Internal LRU node: value plus optional absolute expiry."""

    __slots__ = ("value", "expires_at", "created_at")

    def __init__(self, value: Any, expires_at: float | None) -> None:
        self.value = value
        self.expires_at = expires_at
        self.created_at = time.monotonic()

    def is_expired(self, now: float) -> bool:
        # ``>=`` so a ``ttl=0`` entry (expiry == insertion instant) is already
        # expired on the next read even when the monotonic clock has not ticked.
        return self.expires_at is not None and now >= self.expires_at


def _expiry(ttl: float | None) -> float | None:
    """Absolute monotonic expiry for *ttl*.

    ``ttl=None`` never expires; ``ttl<=0`` is treated as already expired (not
    the same as ``None``), so callers can force an immediate eviction.
    """
    return None if ttl is None else time.monotonic() + float(ttl)


class BoundedLRU:
    """Thread-safe bounded LRU cache with per-entry TTL.

    Recency is tracked with a plain ``dict`` (insertion-ordered on CPython 3.7+):
    a hit deletes and re-inserts the key so the most-recently-used entry is
    always last, and eviction pops the first (oldest) item — both O(1).
    """

    __slots__ = (
        "_data",
        "_max_size",
        "_lock",
        "_hits",
        "_misses",
        "_sets",
        "_lru_evictions",
        "_expirations",
    )

    def __init__(self, max_size: int) -> None:
        self._data: dict[str, _Entry] = {}
        self._max_size = max(1, int(max_size))
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0
        self._sets = 0
        self._lru_evictions = 0
        self._expirations = 0

    # -- core ops ---------------------------------------------------------

    def get(self, key: str, default: Any = None) -> tuple[Any, bool]:
        """Return ``(value, hit)``. Expired entries are evicted and count as a miss."""
        now = time.monotonic()
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                self._misses += 1
                return default, False
            if entry.is_expired(now):
                del self._data[key]
                self._expirations += 1
                self._misses += 1
                return default, False
            # Move to MRU end: delete + reinsert (dict preserves order).
            del self._data[key]
            self._data[key] = entry
            self._hits += 1
            return entry.value, True

    def peek(self, key: str) -> tuple[Any, bool]:
        """Like :meth:`get` but without touching recency or hit/miss counters."""
        now = time.monotonic()
        with self._lock:
            entry = self._data.get(key)
            if entry is None or entry.is_expired(now):
                return None, False
            return entry.value, True

    def set(self, key: str, value: Any, ttl: float | None = None) -> int:
        """Insert/update *key*; evict LRU entries past ``max_size``.

        Returns the number of entries evicted for capacity (not expiry).
        """
        expires_at = _expiry(ttl)
        with self._lock:
            if key in self._data:
                del self._data[key]
            self._data[key] = _Entry(value, expires_at)
            self._sets += 1
            evicted = 0
            while len(self._data) > self._max_size:
                self._data.pop(next(iter(self._data)))
                evicted += 1
            self._lru_evictions += evicted
            return evicted

    def update(
        self,
        key: str,
        updater: Any,
        *,
        default: Any = None,
        ttl: float | None = None,
    ) -> tuple[Any, int]:
        """Atomically read-modify-write *key* under the lock.

        ``updater`` receives the current value (or *default* when absent or
        expired) and returns the new value. Returns ``(new_value, evicted)``.
        Doing this in one locked section prevents lost updates under concurrent
        increments that a ``get`` + ``set`` pair would suffer.
        """
        now = time.monotonic()
        with self._lock:
            entry = self._data.get(key)
            if entry is None or entry.is_expired(now):
                if entry is not None:
                    del self._data[key]
                    self._expirations += 1
                self._misses += 1
                current = default
            else:
                current = entry.value
                del self._data[key]
                self._hits += 1
            value = updater(current)
            self._data[key] = _Entry(value, _expiry(ttl))
            self._sets += 1
            evicted = 0
            while len(self._data) > self._max_size:
                self._data.pop(next(iter(self._data)))
                evicted += 1
            self._lru_evictions += evicted
            return value, evicted

    def delete(self, key: str) -> bool:
        with self._lock:
            if key in self._data:
                del self._data[key]
                return True
            return False

    def exists(self, key: str) -> bool:
        return self.peek(key)[1]

    def clear(self) -> int:
        with self._lock:
            count = len(self._data)
            self._data.clear()
            return count

    # -- bulk / maintenance ----------------------------------------------

    def keys(self, pattern: str | None = None) -> list[str]:
        now = time.monotonic()
        with self._lock:
            live = [k for k, v in self._data.items() if not v.is_expired(now)]
        if pattern is None or pattern == "*":
            return live
        return [k for k in live if fnmatch.fnmatch(k, pattern)]

    def items(self) -> list[tuple[str, Any]]:
        """Snapshot of ``(key, value)`` for non-expired entries (for tag scans)."""
        now = time.monotonic()
        with self._lock:
            return [(k, v.value) for k, v in self._data.items() if not v.is_expired(now)]

    def delete_prefix(self, prefix: str) -> int:
        """Delete every key starting with *prefix* in a single locked pass."""
        with self._lock:
            victims = [k for k in self._data if k.startswith(prefix)]
            for k in victims:
                del self._data[k]
            return len(victims)

    def sweep_expired(self) -> int:
        """Proactively drop expired entries; returns the count removed."""
        now = time.monotonic()
        with self._lock:
            victims = [k for k, v in self._data.items() if v.is_expired(now)]
            for k in victims:
                del self._data[k]
            self._expirations += len(victims)
            return len(victims)

    def resize(self, max_size: int) -> int:
        """Change the capacity bound, evicting LRU entries if shrinking."""
        with self._lock:
            self._max_size = max(1, int(max_size))
            evicted = 0
            while len(self._data) > self._max_size:
                self._data.pop(next(iter(self._data)))
                evicted += 1
            self._lru_evictions += evicted
            return evicted

    # -- introspection ----------------------------------------------------

    @property
    def size(self) -> int:
        return len(self._data)

    @property
    def max_size(self) -> int:
        return self._max_size

    @property
    def stats(self) -> dict[str, Any]:
        total = self._hits + self._misses
        return {
            "size": len(self._data),
            "max_size": self._max_size,
            "hits": self._hits,
            "misses": self._misses,
            "sets": self._sets,
            "lru_evictions": self._lru_evictions,
            "expirations": self._expirations,
            "hit_rate": round(self._hits / total, 4) if total else 0.0,
        }


class ShardedLRU:
    """Capacity-bounded LRU split across ``shards`` independent :class:`BoundedLRU`.

    Keys are routed by a stable hash so each shard carries its own lock —
    concurrent access to different shards never contends. LRU is enforced
    per-shard (approximate global LRU), the standard trade-off for sharded
    caches. Presents the same surface as :class:`BoundedLRU`, so callers are
    agnostic to which they hold.
    """

    __slots__ = ("_shards", "_n")

    def __init__(self, max_size: int, shards: int = 8) -> None:
        # Distribute capacity exactly (quotient + remainder) so the aggregate
        # ``max_size`` equals the requested value with no truncation loss.
        n = min(max(1, int(shards)), max(1, int(max_size)))
        base, extra = divmod(max(1, int(max_size)), n)
        self._shards = [
            BoundedLRU(base + (1 if index < extra else 0))
            for index in range(n)
        ]
        self._n = n

    def _shard(self, key: str) -> BoundedLRU:
        return self._shards[hash(key) % self._n]

    def get(self, key: str, default: Any = None) -> tuple[Any, bool]:
        return self._shard(key).get(key, default)

    def peek(self, key: str) -> tuple[Any, bool]:
        return self._shard(key).peek(key)

    def set(self, key: str, value: Any, ttl: float | None = None) -> int:
        return self._shard(key).set(key, value, ttl)

    def update(
        self,
        key: str,
        updater: Any,
        *,
        default: Any = None,
        ttl: float | None = None,
    ) -> tuple[Any, int]:
        return self._shard(key).update(key, updater, default=default, ttl=ttl)

    def delete(self, key: str) -> bool:
        return self._shard(key).delete(key)

    def exists(self, key: str) -> bool:
        return self._shard(key).exists(key)

    def clear(self) -> int:
        return sum(s.clear() for s in self._shards)

    def keys(self, pattern: str | None = None) -> list[str]:
        out: list[str] = []
        for s in self._shards:
            out.extend(s.keys(pattern))
        return out

    def items(self) -> list[tuple[str, Any]]:
        out: list[tuple[str, Any]] = []
        for s in self._shards:
            out.extend(s.items())
        return out

    def delete_prefix(self, prefix: str) -> int:
        return sum(s.delete_prefix(prefix) for s in self._shards)

    def sweep_expired(self) -> int:
        return sum(s.sweep_expired() for s in self._shards)

    def resize(self, max_size: int) -> int:
        if int(max_size) < self._n:
            raise ValueError(
                f"sharded max_size ({max_size}) must be >= shard count ({self._n})"
            )
        base, extra = divmod(int(max_size), self._n)
        return sum(
            shard.resize(base + (1 if index < extra else 0))
            for index, shard in enumerate(self._shards)
        )

    @property
    def size(self) -> int:
        return sum(s.size for s in self._shards)

    @property
    def max_size(self) -> int:
        return sum(s.max_size for s in self._shards)

    @property
    def stats(self) -> dict[str, Any]:
        agg: dict[str, int | float] = {
            "size": 0,
            "max_size": 0,
            "hits": 0,
            "misses": 0,
            "sets": 0,
            "lru_evictions": 0,
            "expirations": 0,
        }
        for s in self._shards:
            st = s.stats
            for k in agg:
                agg[k] += st[k]
        total = agg["hits"] + agg["misses"]
        agg["hit_rate"] = round(agg["hits"] / total, 4) if total else 0.0
        agg["shards"] = self._n
        return agg


def make_lru(max_size: int, shards: int = 1) -> BoundedLRU | ShardedLRU:
    """Return a :class:`BoundedLRU` (``shards<=1``) or :class:`ShardedLRU`.

    A single shard keeps exact global-LRU semantics (the default, so eviction
    order is deterministic for tests); ``shards>1`` trades exactness for lower
    lock contention.
    """
    if shards and shards > 1:
        return ShardedLRU(max_size, shards)
    return BoundedLRU(max_size)
