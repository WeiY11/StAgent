"""SQLite dashed/hex UUID compatibility for repository-backed paths."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text

from leagent.api.v1.admin.tasks import cancel_task_admin, list_tasks_admin
from leagent.api.v1.activities import get_activities
from leagent.api.v1.stats import get_dashboard_stats, get_home_stats, get_usage_data
from leagent.api.v1.tasks import get_task_files, list_tasks
from leagent.config.settings import Settings
from leagent.db.models.task import TaskStatus
from leagent.db.service import DatabaseService
from leagent.services.task_manager import TaskManager
from leagent.workflow.services import WorkflowService


@pytest_asyncio.fixture()
async def repo_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    db_path = tmp_path / "repositories.db"
    monkeypatch.setenv("DB_DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    db = DatabaseService(Settings())
    await db.create_tables()
    try:
        yield db
    finally:
        await db.dispose()


async def _insert_dashed_repository_rows(
    db: DatabaseService,
    *,
    user_id: UUID,
    other_user_id: UUID,
    session_id: UUID,
    message_id: UUID,
    file_id: UUID,
    other_file_id: UUID,
    delete_file_id: UUID,
    task_id: UUID,
    flow_id: UUID,
    workflow_execution_id: UUID,
    storage_dir: Path,
) -> None:
    now = datetime.utcnow().isoformat(sep=" ")
    output_data = json.dumps(
        {"files": [str(file_id), str(other_file_id), str(delete_file_id)]}
    )
    async with db.session() as session:
        await session.execute(
            text("INSERT INTO users (id, role, disabled) VALUES (:id, 'user', 0)"),
            {"id": str(user_id)},
        )
        await session.execute(
            text("INSERT INTO users (id, role, disabled) VALUES (:id, 'user', 0)"),
            {"id": str(other_user_id)},
        )
        await session.execute(
            text(
                "INSERT INTO chat_sessions "
                "(created_at, updated_at, id, name, user_id, is_active, "
                "message_count, last_message_at) "
                "VALUES (:now, :now, :id, 'repo chat', :uid, 1, 1, :now)"
            ),
            {"now": now, "id": str(session_id), "uid": str(user_id)},
        )
        await session.execute(
            text(
                "INSERT INTO messages "
                "(created_at, updated_at, id, role, content, status, session_id, user_id) "
                "VALUES (:now, :now, :id, 'user', 'hello', 'completed', :sid, :uid)"
            ),
            {
                "now": now,
                "id": str(message_id),
                "sid": str(session_id),
                "uid": str(user_id),
            },
        )
        await session.execute(
            text(
                "INSERT INTO flows "
                "(is_deleted, created_at, updated_at, id, name, status, flow_type, "
                "is_public, version, user_id, run_count) "
                "VALUES (0, :now, :now, :id, 'Repo Flow', 'draft', 'workflow', "
                "0, 1, :uid, 0)"
            ),
            {"now": now, "id": str(flow_id), "uid": str(user_id)},
        )

        for fid, uid, name, sid, deleted in [
            (file_id, user_id, "owned.txt", session_id, 0),
            (other_file_id, other_user_id, "other.txt", session_id, 0),
            (delete_file_id, user_id, "delete.txt", None, 0),
        ]:
            await session.execute(
                text(
                    "INSERT INTO files "
                    "(is_deleted, created_at, updated_at, id, name, original_name, "
                    "file_type, size, status, user_id, session_id, storage_path, "
                    "has_ocr, is_indexed, library_scope, inbox_state, is_pinned) "
                    "VALUES (:deleted, :now, :now, :id, :name, :name, 'document', "
                    "1, 'processed', :uid, :sid, :path, 0, 1, 'knowledge', 'new', 0)"
                ),
                {
                    "deleted": deleted,
                    "now": now,
                    "id": str(fid),
                    "name": name,
                    "uid": str(uid),
                    "sid": str(sid) if sid is not None else None,
                    "path": str(storage_dir / name),
                },
            )

        await session.execute(
            text(
                "INSERT INTO tasks "
                "(created_at, updated_at, id, name, task_type, status, priority, "
                "user_id, flow_id, session_id, output_data, progress, retry_count, "
                "max_retries, timeout_seconds, output_offset, notified, total_paused_ms) "
                "VALUES (:now, :now, :id, 'repo task', 'agent', 'running', 'normal', "
                ":uid, :flow_id, :sid, :output_data, 5, 0, 3, 300, 0, 0, 0)"
            ),
            {
                "now": now,
                "id": str(task_id),
                "uid": str(user_id),
                "flow_id": str(flow_id),
                "sid": str(session_id),
                "output_data": output_data,
            },
        )
        await session.execute(
            text(
                "INSERT INTO workflow_executions "
                "(id, flow_id, user_id, prompt_id, priority, status, trigger_type, "
                "node_count, duration_ms, retry_count, created_at, updated_at) "
                "VALUES (:id, :flow_id, :uid, 'repo-prompt', 5, 'running', "
                "'manual', 1, 0, 0, :now, :now)"
            ),
            {
                "id": str(workflow_execution_id),
                "flow_id": str(flow_id),
                "uid": str(user_id),
                "now": now,
            },
        )


@pytest.mark.asyncio
async def test_repositories_match_dashed_sqlite_uuid_rows(
    repo_db: DatabaseService,
    tmp_path: Path,
) -> None:
    user_id = uuid4()
    other_user_id = uuid4()
    session_id = uuid4()
    message_id = uuid4()
    file_id = uuid4()
    other_file_id = uuid4()
    delete_file_id = uuid4()
    task_id = uuid4()
    flow_id = uuid4()
    workflow_execution_id = uuid4()
    await _insert_dashed_repository_rows(
        repo_db,
        user_id=user_id,
        other_user_id=other_user_id,
        session_id=session_id,
        message_id=message_id,
        file_id=file_id,
        other_file_id=other_file_id,
        delete_file_id=delete_file_id,
        task_id=task_id,
        flow_id=flow_id,
        workflow_execution_id=workflow_execution_id,
        storage_dir=tmp_path,
    )

    files = repo_db.repositories.files
    assert (await files.get(file_id)).id == file_id  # type: ignore[union-attr]
    assert (await files.get_for_user(file_id, user_id)).id == file_id  # type: ignore[union-attr]
    assert await files.get_for_user(file_id, other_user_id) is None
    assert [row.id for row in await files.list_for_session(session_id)] == [
        file_id,
        other_file_id,
    ]
    assert [row.id for row in await files.get_many_for_user(
        [file_id, other_file_id],
        user_id,
    )] == [file_id]
    assert await files.soft_delete(delete_file_id, user_id) is True
    assert await files.soft_delete(other_file_id, user_id) is False

    tasks = repo_db.repositories.tasks
    assert (await tasks.get(task_id)).id == task_id  # type: ignore[union-attr]
    assert [row.id for row in await tasks.list_for_user(user_id)] == [task_id]
    assert await tasks.update_status(task_id, TaskStatus.COMPLETED) is True

    chat = repo_db.repositories.chat
    assert (await chat.get_session(session_id)).id == session_id  # type: ignore[union-attr]
    assert (await chat.get_session_for_user(session_id, user_id)).id == session_id  # type: ignore[union-attr]
    assert await chat.get_session_for_user(session_id, other_user_id) is None
    assert [row.id for row in await chat.list_sessions_for_user(user_id)] == [session_id]
    assert [row.id for row in await chat.list_messages(session_id)] == [message_id]

    executions = repo_db.repositories.workflow_executions
    assert (await executions.get(workflow_execution_id)).id == workflow_execution_id  # type: ignore[union-attr]
    assert [row.id for row in await executions.list_for_flow(flow_id)] == [
        workflow_execution_id
    ]
    assert [row.id for row in await executions.list_for_user(user_id)] == [
        workflow_execution_id
    ]

    async with repo_db.session() as session:
        deleted = (
            await session.execute(
                text("SELECT is_deleted FROM files WHERE id = :id"),
                {"id": str(delete_file_id)},
            )
        ).scalar_one()
        status = (
            await session.execute(
                text("SELECT status FROM tasks WHERE id = :id"),
                {"id": str(task_id)},
            )
        ).scalar_one()

    assert bool(deleted) is True
    assert status == TaskStatus.COMPLETED.value


@pytest.mark.asyncio
async def test_task_api_paths_match_dashed_sqlite_uuid_rows(
    repo_db: DatabaseService,
    tmp_path: Path,
) -> None:
    user_id = uuid4()
    other_user_id = uuid4()
    session_id = uuid4()
    message_id = uuid4()
    file_id = uuid4()
    other_file_id = uuid4()
    delete_file_id = uuid4()
    task_id = uuid4()
    flow_id = uuid4()
    workflow_execution_id = uuid4()
    await _insert_dashed_repository_rows(
        repo_db,
        user_id=user_id,
        other_user_id=other_user_id,
        session_id=session_id,
        message_id=message_id,
        file_id=file_id,
        other_file_id=other_file_id,
        delete_file_id=delete_file_id,
        task_id=task_id,
        flow_id=flow_id,
        workflow_execution_id=workflow_execution_id,
        storage_dir=tmp_path,
    )

    listed = await list_tasks(
        user_id,
        repo_db,
        page=1,
        page_size=10,
        status=None,
        task_type=None,
        priority=None,
        flow_id=None,
        search=None,
    )
    task_files = await get_task_files(task_id, user_id, repo_db)

    assert listed.total == 2
    assert [item.id for item in listed.items] == [task_id, workflow_execution_id]
    assert [item.name for item in listed.items] == ["repo task", "Repo Flow"]
    assert [item.id for item in task_files] == [file_id, delete_file_id]


@pytest.mark.asyncio
async def test_workflow_service_matches_and_updates_dashed_sqlite_uuid_rows(
    repo_db: DatabaseService,
    tmp_path: Path,
) -> None:
    user_id = uuid4()
    other_user_id = uuid4()
    session_id = uuid4()
    message_id = uuid4()
    file_id = uuid4()
    other_file_id = uuid4()
    delete_file_id = uuid4()
    task_id = uuid4()
    flow_id = uuid4()
    workflow_execution_id = uuid4()
    await _insert_dashed_repository_rows(
        repo_db,
        user_id=user_id,
        other_user_id=other_user_id,
        session_id=session_id,
        message_id=message_id,
        file_id=file_id,
        other_file_id=other_file_id,
        delete_file_id=delete_file_id,
        task_id=task_id,
        flow_id=flow_id,
        workflow_execution_id=workflow_execution_id,
        storage_dir=tmp_path,
    )
    service = WorkflowService(repo_db, MagicMock(), MagicMock(), queue=None)

    execution = await service.get_execution(workflow_execution_id)
    executions_for_flow = await service.list_executions(flow_id)
    paused = await service.pause_execution(workflow_execution_id)
    await service.update_execution_by_prompt_id(
        prompt_id="repo-prompt",
        status="completed",
        outputs={"ok": True},
        duration_ms=25,
    )
    completed = await service.get_execution(workflow_execution_id)

    assert execution is not None
    assert execution["id"] == str(workflow_execution_id)
    assert execution["status"] == "running"
    assert [row["id"] for row in executions_for_flow] == [str(workflow_execution_id)]
    assert paused is True
    assert completed is not None
    assert completed["status"] == "completed"
    assert completed["outputs"] == {"ok": True}


@pytest.mark.asyncio
async def test_dashboard_and_activity_paths_match_dashed_sqlite_uuid_rows(
    repo_db: DatabaseService,
    tmp_path: Path,
) -> None:
    user_id = uuid4()
    other_user_id = uuid4()
    session_id = uuid4()
    message_id = uuid4()
    file_id = uuid4()
    other_file_id = uuid4()
    delete_file_id = uuid4()
    task_id = uuid4()
    flow_id = uuid4()
    workflow_execution_id = uuid4()
    await _insert_dashed_repository_rows(
        repo_db,
        user_id=user_id,
        other_user_id=other_user_id,
        session_id=session_id,
        message_id=message_id,
        file_id=file_id,
        other_file_id=other_file_id,
        delete_file_id=delete_file_id,
        task_id=task_id,
        flow_id=flow_id,
        workflow_execution_id=workflow_execution_id,
        storage_dir=tmp_path,
    )

    now = datetime.utcnow().isoformat(sep=" ")
    completed_task_id = uuid4()
    completed_wf_id = uuid4()
    other_completed_task_id = uuid4()
    other_completed_wf_id = uuid4()
    async with repo_db.session() as session:
        await session.execute(
            text(
                "INSERT INTO tasks "
                "(created_at, updated_at, id, name, task_type, status, priority, "
                "user_id, flow_id, progress, retry_count, max_retries, completed_at, "
                "duration_ms, timeout_seconds, output_offset, notified, total_paused_ms) "
                "VALUES (:now, :now, :id, 'done task', 'agent', 'completed', "
                "'normal', :uid, :flow_id, 100, 0, 3, :now, 1000, 300, 0, 0, 0)"
            ),
            {
                "now": now,
                "id": str(completed_task_id),
                "uid": str(user_id),
                "flow_id": str(flow_id),
            },
        )
        await session.execute(
            text(
                "INSERT INTO tasks "
                "(created_at, updated_at, id, name, task_type, status, priority, "
                "user_id, progress, retry_count, max_retries, completed_at, "
                "duration_ms, timeout_seconds, output_offset, notified, total_paused_ms) "
                "VALUES (:now, :now, :id, 'other done task', 'agent', 'completed', "
                "'normal', :uid, 100, 0, 3, :now, 1000, 300, 0, 0, 0)"
            ),
            {
                "now": now,
                "id": str(other_completed_task_id),
                "uid": str(other_user_id),
            },
        )
        await session.execute(
            text(
                "INSERT INTO workflow_executions "
                "(id, flow_id, user_id, priority, status, trigger_type, node_count, "
                "duration_ms, retry_count, completed_at, created_at, updated_at) "
                "VALUES (:id, :flow_id, :uid, 5, 'completed', 'manual', 1, 2000, "
                "0, :now, :now, :now)"
            ),
            {
                "id": str(completed_wf_id),
                "flow_id": str(flow_id),
                "uid": str(user_id),
                "now": now,
            },
        )
        await session.execute(
            text(
                "INSERT INTO workflow_executions "
                "(id, user_id, priority, status, trigger_type, node_count, "
                "duration_ms, retry_count, completed_at, created_at, updated_at) "
                "VALUES (:id, :uid, 5, 'completed', 'manual', 1, 2000, "
                "0, :now, :now, :now)"
            ),
            {"id": str(other_completed_wf_id), "uid": str(other_user_id), "now": now},
        )

    home = await get_home_stats(user_id, repo_db)
    dashboard = await get_dashboard_stats(user_id, repo_db, timeRange="today")
    usage = await get_usage_data(user_id, repo_db, timeRange="today")
    activities = await get_activities(user_id, repo_db, limit=10)

    assert home.totalFlows == 1
    assert home.runningTasks == 2
    assert home.completedToday == 2
    assert home.successRate == 100.0
    assert dashboard.tasksToday == 4
    assert dashboard.successRate == 100.0
    assert dashboard.failedTasks == 0
    assert sum(point.success for point in usage) == 2
    assert sum(point.failed for point in usage) == 0
    assert {activity.id for activity in activities} >= {
        str(task_id),
        str(workflow_execution_id),
        str(completed_task_id),
        str(completed_wf_id),
    }
    assert "Repo Flow" in {activity.title for activity in activities}


@pytest.mark.asyncio
async def test_task_manager_and_admin_paths_update_dashed_sqlite_uuid_rows(
    repo_db: DatabaseService,
    tmp_path: Path,
) -> None:
    user_id = uuid4()
    other_user_id = uuid4()
    session_id = uuid4()
    message_id = uuid4()
    file_id = uuid4()
    other_file_id = uuid4()
    delete_file_id = uuid4()
    task_id = uuid4()
    flow_id = uuid4()
    workflow_execution_id = uuid4()
    await _insert_dashed_repository_rows(
        repo_db,
        user_id=user_id,
        other_user_id=other_user_id,
        session_id=session_id,
        message_id=message_id,
        file_id=file_id,
        other_file_id=other_file_id,
        delete_file_id=delete_file_id,
        task_id=task_id,
        flow_id=flow_id,
        workflow_execution_id=workflow_execution_id,
        storage_dir=tmp_path,
    )
    manager = TaskManager()

    admin_list = await list_tasks_admin(
        repo_db,
        page=1,
        page_size=10,
        status_filter=None,
        task_type=None,
        priority=None,
        user_id=user_id,
        flow_id=flow_id,
        search=None,
    )
    async with repo_db.session() as session:
        cancelled = await manager.cancel_task(session, str(task_id))

    async with repo_db.session() as session:
        after_manager_cancel = (
            await session.execute(
                text("SELECT status FROM tasks WHERE id = :id"),
                {"id": str(task_id)},
            )
        ).scalar_one()
        await session.execute(
            text(
                "UPDATE tasks SET status = 'running', completed_at = NULL "
                "WHERE id = :id"
            ),
            {"id": str(task_id)},
        )

    async with repo_db.session() as session:
        killed = await manager.kill_task(session, str(task_id))

    async with repo_db.session() as session:
        after_manager_kill = (
            await session.execute(
                text("SELECT status FROM tasks WHERE id = :id"),
                {"id": str(task_id)},
            )
        ).scalar_one()
        await session.execute(
            text(
                "UPDATE tasks SET status = 'running', completed_at = NULL "
                "WHERE id = :id"
            ),
            {"id": str(task_id)},
        )

    admin_cancel = await cancel_task_admin(task_id, repo_db)

    async with repo_db.session() as session:
        after_admin_cancel = (
            await session.execute(
                text("SELECT status FROM tasks WHERE id = :id"),
                {"id": str(task_id)},
            )
        ).scalar_one()

    assert admin_list.total == 1
    assert [item.id for item in admin_list.items] == [task_id]
    assert cancelled is True
    assert after_manager_cancel == TaskStatus.CANCELLED.value
    assert killed is True
    assert after_manager_kill == TaskStatus.KILLED.value
    assert admin_cancel.ok is True
    assert admin_cancel.previous_status == TaskStatus.RUNNING.value
    assert after_admin_cancel == TaskStatus.CANCELLED.value
