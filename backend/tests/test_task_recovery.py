"""Background task recovery contracts."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from contextlib import asynccontextmanager, suppress
from typing import Any, AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool
from sqlmodel.ext.asyncio.session import AsyncSession

from StAgent.db.models.task import Task, TaskContext, TaskStatus, TaskType
from StAgent.services.task_manager import TaskManager
from StAgent.tasks.jobs import recover_pending_jobs


def _sqlite_connect_pragma(dbapi_conn: object, _: object) -> None:
    cursor = dbapi_conn.cursor()  # type: ignore[union-attr]
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA busy_timeout=15000")
    cursor.close()


class _TaskDB:
    def __init__(self) -> None:
        fd, path = tempfile.mkstemp(suffix=".db", prefix="task_recovery_")
        os.close(fd)
        self._path = path
        self._engine = create_async_engine(
            f"sqlite+aiosqlite:///{path}",
            echo=False,
            poolclass=NullPool,
            connect_args={"check_same_thread": False, "timeout": 15},
        )
        event.listen(self._engine.sync_engine, "connect", _sqlite_connect_pragma)
        self._factory = sessionmaker(
            self._engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )

    async def prepare(self) -> None:
        async with self._engine.begin() as conn:
            await conn.run_sync(
                lambda sync_conn: Task.__table__.create(sync_conn, checkfirst=True)
            )

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self._factory() as session:
            try:
                yield session
                await session.commit()
            except (Exception, asyncio.CancelledError):
                await session.rollback()
                raise

    async def dispose(self) -> None:
        await self._engine.dispose()
        with suppress(OSError):
            os.remove(self._path)


@pytest_asyncio.fixture()
async def task_db(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[_TaskDB]:
    db = _TaskDB()
    await db.prepare()

    from StAgent import db as db_pkg

    monkeypatch.setattr(db_pkg, "get_database_service", lambda: db, raising=True)
    yield db
    await db.dispose()


class _CaptureHandler:
    name = "capture_handler"

    def __init__(self, task_type: TaskType) -> None:
        self.task_type = task_type
        self.calls: list[dict[str, Any]] = []

    async def spawn(
        self,
        task_ctx: TaskContext,
        params: dict[str, Any],
        session: AsyncSession,
    ) -> dict[str, Any]:
        del session
        self.calls.append(dict(params))
        task_ctx.append_output(json.dumps({"event": "recovered", **params}) + "\n")
        return {"recovered": True, "task_type": self.task_type.value}

    async def kill(self, task_id: str, session: AsyncSession) -> None:
        return None


async def _wait_until(predicate, *, timeout: float = 5.0) -> None:
    end = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < end:
        if await predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition never became true")


@pytest.mark.asyncio
async def test_recover_pending_jobs_uses_registered_handler_types(
    task_db: _TaskDB,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from StAgent.services import task_manager as task_manager_module

    shell = _CaptureHandler(TaskType.SHELL)
    tool = _CaptureHandler(TaskType.TOOL)
    agent = _CaptureHandler(TaskType.AGENT)
    mgr = TaskManager()
    for handler in (shell, tool, agent):
        mgr.register_handler(handler)
    monkeypatch.setattr(task_manager_module, "_task_manager", mgr)

    async with task_db.session() as session:
        tasks = [
            Task(
                name="recover-shell",
                task_type=TaskType.SHELL,
                status=TaskStatus.PENDING,
                input_data=json.dumps({"marker": "shell"}),
            ),
            Task(
                name="recover-tool",
                task_type=TaskType.TOOL,
                status=TaskStatus.QUEUED,
                input_data=json.dumps({"marker": "tool"}),
            ),
            Task(
                name="recover-agent",
                task_type=TaskType.AGENT,
                status=TaskStatus.RUNNING,
                input_data=json.dumps({"marker": "agent"}),
            ),
            Task(
                name="ignore-export",
                task_type=TaskType.EXPORT,
                status=TaskStatus.PENDING,
                input_data=json.dumps({"marker": "export"}),
            ),
            Task(
                name="ignore-completed",
                task_type=TaskType.SHELL,
                status=TaskStatus.COMPLETED,
                input_data=json.dumps({"marker": "completed"}),
            ),
        ]
        session.add_all(tasks)
        await session.flush()
        recovered_ids = {tasks[0].id, tasks[1].id, tasks[2].id}
        ignored_export_id = tasks[3].id
        ignored_completed_id = tasks[4].id

    assert await recover_pending_jobs(task_db) == 3

    async def _all_recovered_complete() -> bool:
        async with task_db.session() as session:
            rows = [await session.get(Task, task_id) for task_id in recovered_ids]
            return all(row is not None and row.status == TaskStatus.COMPLETED for row in rows)

    await _wait_until(_all_recovered_complete)

    async with task_db.session() as session:
        ignored_export = await session.get(Task, ignored_export_id)
        ignored_completed = await session.get(Task, ignored_completed_id)
        assert ignored_export is not None
        assert ignored_completed is not None
        assert ignored_export.status == TaskStatus.PENDING
        assert ignored_completed.status == TaskStatus.COMPLETED

    assert [call["marker"] for call in shell.calls] == ["shell"]
    assert [call["marker"] for call in tool.calls] == ["tool"]
    assert [call["marker"] for call in agent.calls] == ["agent"]


@pytest.mark.asyncio
async def test_recover_pending_jobs_skips_locally_running_task(
    task_db: _TaskDB,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from StAgent.services import task_manager as task_manager_module

    shell = _CaptureHandler(TaskType.SHELL)
    mgr = TaskManager()
    mgr.register_handler(shell)
    monkeypatch.setattr(task_manager_module, "_task_manager", mgr)

    async with task_db.session() as session:
        task = Task(
            name="already-running",
            task_type=TaskType.SHELL,
            status=TaskStatus.RUNNING,
            input_data=json.dumps({"marker": "skip"}),
        )
        session.add(task)
        await session.flush()
        task_id = task.id

    sleeper = asyncio.create_task(asyncio.sleep(60))
    mgr._running_tasks[str(task_id)] = sleeper
    try:
        assert await recover_pending_jobs(task_db) == 0
        assert shell.calls == []
    finally:
        sleeper.cancel()
        with suppress(asyncio.CancelledError):
            await sleeper
