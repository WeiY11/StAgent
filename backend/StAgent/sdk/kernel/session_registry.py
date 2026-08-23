"""Session task registry — tracks in-flight agent runs per session.

This module extracts the session-level task tracking logic from
AgentController into a standalone registry. The registry maintains:

- Active task events (for abort signaling)
- Task metadata records (for status queries)
- Durable checkpoint handles (for resume/cancel coordination)

All state is module-level (class attributes on SessionTaskRegistry)
so every AgentController instance in the same process shares the
registry — matching the original class-level state semantics.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    pass


@dataclass
class AgentRunTaskRecord:
    """In-process metadata for one concurrent agent HTTP/stream run."""

    task_id: UUID
    session_id: UUID
    user_id: UUID | None
    started_at: datetime
    updated_at: datetime
    phase: str = "starting"
    tool_name: str | None = None
    status: str = "running"


class SessionTaskRegistry:
    """Module-level registry for in-flight agent tasks.

    All methods are classmethods so callers can use either
    ``SessionTaskRegistry.method()`` or instantiate and call on an
    instance (for backward compatibility with AgentController's
    classmethod API).
    """

    _session_tasks: dict[UUID, dict[UUID, asyncio.Event]] = {}
    _session_task_records: dict[UUID, dict[UUID, AgentRunTaskRecord]] = {}
    _session_last_checkpoint: dict[UUID, str] = {}
    _session_checkpoint_events: dict[UUID, asyncio.Event] = {}

    @classmethod
    def register(
        cls,
        session_id: UUID,
        task_id: UUID,
        abort_event: asyncio.Event,
        user_id: UUID | None,
    ) -> None:
        """Register a new agent task for a session."""
        now = datetime.utcnow()
        cls._session_tasks.setdefault(session_id, {})[task_id] = abort_event
        cls._session_task_records.setdefault(session_id, {})[task_id] = AgentRunTaskRecord(
            task_id=task_id,
            session_id=session_id,
            user_id=user_id,
            started_at=now,
            updated_at=now,
            phase="starting",
            tool_name=None,
            status="running",
        )
        # Fresh wait handle for this turn's checkpoint (cancel may await it).
        cls._session_last_checkpoint.pop(session_id, None)
        cls._session_checkpoint_events[session_id] = asyncio.Event()

    @classmethod
    def unregister(cls, session_id: UUID, task_id: UUID) -> None:
        """Remove a task from the registry."""
        sess = cls._session_tasks.get(session_id)
        if sess and task_id in sess:
            sess.pop(task_id)
            if not sess:
                cls._session_tasks.pop(session_id, None)
        recs = cls._session_task_records.get(session_id)
        if recs and task_id in recs:
            recs.pop(task_id)
            if not recs:
                cls._session_task_records.pop(session_id, None)

    @classmethod
    def update_phase(
        cls,
        session_id: UUID,
        task_id: UUID,
        phase: str,
        tool_name: str | None = None,
    ) -> None:
        """Update the phase (and optionally tool name) of a running task."""
        recs = cls._session_task_records.get(session_id)
        if not recs:
            return
        rec = recs.get(task_id)
        if rec is None:
            return
        rec.phase = phase
        rec.updated_at = datetime.utcnow()
        if tool_name is not None:
            rec.tool_name = tool_name

    @classmethod
    def stash_checkpoint(cls, session_id: UUID, checkpoint_id: str | None) -> None:
        """Record a durable checkpoint id for a session (abort / pause)."""
        if not checkpoint_id or not session_id:
            return
        cls._session_last_checkpoint[session_id] = checkpoint_id
        ev = cls._session_checkpoint_events.get(session_id)
        if ev is None:
            ev = asyncio.Event()
            cls._session_checkpoint_events[session_id] = ev
        ev.set()

    @classmethod
    def peek_checkpoint(cls, session_id: UUID) -> str | None:
        """Return the last checkpoint id for a session (if any)."""
        return cls._session_last_checkpoint.get(session_id)

    @classmethod
    async def wait_checkpoint(
        cls,
        session_id: UUID,
        *,
        timeout: float = 2.0,
    ) -> str | None:
        """Wait briefly for an in-flight abort path to persist a checkpoint."""
        existing = cls._session_last_checkpoint.get(session_id)
        if existing:
            return existing
        ev = cls._session_checkpoint_events.get(session_id)
        if ev is None:
            return None
        try:
            await asyncio.wait_for(ev.wait(), timeout=max(0.0, timeout))
        except TimeoutError:
            pass
        return cls._session_last_checkpoint.get(session_id)

    @classmethod
    def cancel_session(cls, session_id: UUID) -> bool:
        """Signal abort on every in-flight task for this session."""
        tasks = cls._session_tasks.get(session_id)
        if not tasks:
            return False
        for ev in list(tasks.values()):
            ev.set()
        cls._session_tasks.pop(session_id, None)
        cls._session_task_records.pop(session_id, None)
        return True

    @classmethod
    def cancel_task(cls, session_id: UUID, task_id: UUID) -> bool:
        """Abort a single agent task within a session."""
        sess = cls._session_tasks.get(session_id)
        if not sess:
            return False
        ev = sess.get(task_id)
        if ev is None:
            return False
        ev.set()
        sess.pop(task_id, None)
        if not sess:
            cls._session_tasks.pop(session_id, None)
        recs = cls._session_task_records.get(session_id)
        if recs and task_id in recs:
            recs.pop(task_id)
            if not recs:
                cls._session_task_records.pop(session_id, None)
        return True

    @classmethod
    def list_tasks(cls, session_id: UUID) -> list[AgentRunTaskRecord]:
        """Return snapshots of running agent tasks for a session."""
        recs = cls._session_task_records.get(session_id)
        if not recs:
            return []
        return sorted(recs.values(), key=lambda r: r.started_at)

    @classmethod
    def is_active(cls, session_id: UUID) -> bool:
        """Return True if the session has any in-flight tasks."""
        return bool(cls._session_tasks.get(session_id))


    # Raw dict accessors are exposed as module-level attributes
    # (_session_tasks, _session_task_records, etc.) so backward-compatible
    # code in AgentController can alias them as class attributes.


__all__ = ["AgentRunTaskRecord", "SessionTaskRegistry"]
