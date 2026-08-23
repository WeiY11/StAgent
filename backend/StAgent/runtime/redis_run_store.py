"""Redis-backed :class:`RunStore` for multi-worker deployments.

This backend lets several worker processes share the active-run registry so that
cross-scope linking, pause/resume lookup and per-session observability keep
working when a chat turn, its sub-agents and its workflow steps are spread
across workers.

It is **opt-in**: the standalone single-worker deployment never constructs it
(``ServiceManager.redis`` is ``None``), so importing this module has no runtime
cost and adds no hard dependency — the redis client is *injected*, never
imported here. A distributed entrypoint builds one via
:func:`build_redis_run_store` and installs it with
:func:`StAgent.runtime.run_store.set_run_store`.

Design notes:
* The client must be a **synchronous** redis client (the :class:`RunStore`
  contract is synchronous; see that module for why).
* Ordering for :meth:`get_active_chat_turn` uses ``ExecutionRun.created_at``
  (wall clock) rather than the process-local monotonic ``registered_at``.
* Every key carries a TTL so an abandoned pause cannot leak forever, mirroring
  the in-process registry's lazy sweep.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from StAgent.runtime.execution_run import ExecutionRun, ExecutionScope
from StAgent.utils.logging import get_logger

if TYPE_CHECKING:
    from StAgent.runtime.run_store import RunStore

logger = get_logger(__name__)

_DEFAULT_PREFIX = "StAgent:runs"
_DEFAULT_TTL_SECONDS = 6 * 60 * 60  # 6 hours, matching the in-process registry


def _to_str(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return str(value)


class RedisRunStore:
    """A :class:`RunStore` whose state lives in Redis.

    ``client`` is any object exposing the synchronous redis-py surface used
    below (``set``/``get``/``delete``/``sadd``/``srem``/``smembers``/
    ``expire``). Injecting it keeps this module import-safe without redis.
    """

    def __init__(
        self,
        client: Any,
        *,
        prefix: str = _DEFAULT_PREFIX,
        ttl_seconds: int = _DEFAULT_TTL_SECONDS,
    ) -> None:
        self._client = client
        self._prefix = prefix
        self._ttl_seconds = ttl_seconds if ttl_seconds and ttl_seconds > 0 else None

    # -- key helpers -------------------------------------------------------
    def _run_key(self, run_id: str) -> str:
        return f"{self._prefix}:run:{run_id}"

    def _prompt_key(self, prompt_id: str) -> str:
        return f"{self._prefix}:prompt:{prompt_id}"

    def _session_key(self, session_id: str) -> str:
        return f"{self._prefix}:session:{session_id}"

    # -- RunStore protocol -------------------------------------------------
    def register(self, run: ExecutionRun) -> ExecutionRun:
        payload = json.dumps(run.to_dict())
        ttl = self._ttl_seconds
        if ttl is not None:
            self._client.set(self._run_key(run.run_id), payload, ex=ttl)
        else:
            self._client.set(self._run_key(run.run_id), payload)
        if run.prompt_id:
            if ttl is not None:
                self._client.set(self._prompt_key(run.prompt_id), run.run_id, ex=ttl)
            else:
                self._client.set(self._prompt_key(run.prompt_id), run.run_id)
        if run.session_id:
            skey = self._session_key(run.session_id)
            self._client.sadd(skey, run.run_id)
            if ttl is not None:
                self._client.expire(skey, ttl)
        return run

    def get(self, run_id: str) -> ExecutionRun | None:
        raw = _to_str(self._client.get(self._run_key(run_id)))
        if raw is None:
            return None
        return ExecutionRun.from_dict(json.loads(raw))

    def remove(self, run_id: str) -> None:
        run = self.get(run_id)
        self._client.delete(self._run_key(run_id))
        if run is None:
            return
        if run.prompt_id:
            # Only drop the prompt index if it still points at this run.
            current = _to_str(self._client.get(self._prompt_key(run.prompt_id)))
            if current == run_id:
                self._client.delete(self._prompt_key(run.prompt_id))
        if run.session_id:
            self._client.srem(self._session_key(run.session_id), run_id)

    def list_for_session(self, session_id: str) -> list[ExecutionRun]:
        members = self._client.smembers(self._session_key(session_id))
        runs: list[ExecutionRun] = []
        for member in members or []:
            run_id = _to_str(member)
            if run_id is None:
                continue
            run = self.get(run_id)
            if run is not None:
                runs.append(run)
            else:
                # Expired/removed run: prune the dangling set member.
                self._client.srem(self._session_key(session_id), run_id)
        return runs

    def get_by_prompt_id(self, prompt_id: str) -> ExecutionRun | None:
        run_id = _to_str(self._client.get(self._prompt_key(prompt_id)))
        if run_id is None:
            return None
        return self.get(run_id)

    def get_active_chat_turn(self, session_id: str) -> ExecutionRun | None:
        matches = [
            r
            for r in self.list_for_session(session_id)
            if r.scope == ExecutionScope.CHAT_TURN
        ]
        if not matches:
            return None
        # Wall-clock ordering is comparable across workers (unlike monotonic).
        return max(matches, key=lambda r: r.created_at)


def build_redis_run_store(client: Any, **kwargs: Any) -> RunStore:
    """Construct a :class:`RedisRunStore` (typed as :class:`RunStore`)."""
    return RedisRunStore(client, **kwargs)


__all__ = ["RedisRunStore", "build_redis_run_store"]
