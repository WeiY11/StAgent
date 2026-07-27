"""workflow_save SQLite UUID and ownership regressions."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text

from leagent.config.settings import Settings
from leagent.db.service import DatabaseService
from leagent.tools.base import ToolContext
from leagent.tools.workflow.workflow_save import WorkflowSaveTool


@pytest_asyncio.fixture()
async def workflow_save_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    db_path = tmp_path / "workflow-save.db"
    monkeypatch.setenv("DB_DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    db = DatabaseService(Settings())
    await db.create_tables()
    try:
        yield db
    finally:
        await db.dispose()


async def _insert_dashed_user(db: DatabaseService, user_id: UUID) -> None:
    async with db.session() as session:
        await session.execute(
            text("INSERT INTO users (id, role, disabled) VALUES (:id, 'user', 0)"),
            {"id": str(user_id)},
        )


async def _insert_dashed_flow(
    db: DatabaseService,
    *,
    flow_id: UUID,
    user_id: UUID,
    document: dict,
    name: str,
) -> None:
    now = datetime.utcnow().isoformat(sep=" ")
    async with db.session() as session:
        await session.execute(
            text(
                """
                INSERT INTO flows (
                    is_deleted, created_at, updated_at, id, name, status,
                    flow_type, is_public, data, version, user_id, run_count
                )
                VALUES (
                    0, :now, :now, :id, :name, 'draft',
                    'workflow', 0, :data, 1, :uid, 0
                )
                """
            ),
            {
                "now": now,
                "id": str(flow_id),
                "name": name,
                "data": json.dumps(document),
                "uid": str(user_id),
            },
        )


@pytest.mark.asyncio
async def test_workflow_save_updates_owned_dashed_flow_and_denies_other_user(
    workflow_save_db: DatabaseService,
    sample_canonical_document: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from leagent.workflow.nodes import bootstrap

    await bootstrap()
    owner_id = uuid4()
    other_id = uuid4()
    owned_flow_id = uuid4()
    other_flow_id = uuid4()
    await _insert_dashed_user(workflow_save_db, owner_id)
    await _insert_dashed_user(workflow_save_db, other_id)
    await _insert_dashed_flow(
        workflow_save_db,
        flow_id=owned_flow_id,
        user_id=owner_id,
        document=sample_canonical_document,
        name="owned old",
    )
    await _insert_dashed_flow(
        workflow_save_db,
        flow_id=other_flow_id,
        user_id=other_id,
        document=sample_canonical_document,
        name="other old",
    )
    monkeypatch.setattr(
        "leagent.services.service_manager.get_service_manager",
        lambda: SimpleNamespace(db=workflow_save_db),
    )
    tool = WorkflowSaveTool()
    context = ToolContext(user_id=str(owner_id), session_id=None)

    denied = await tool._execute(
        context,
        name="stolen",
        flow_data=sample_canonical_document,
        flow_id=str(other_flow_id),
    )
    updated = await tool._execute(
        context,
        name="owned new",
        flow_data=sample_canonical_document,
        flow_id=str(owned_flow_id),
    )
    created = await tool._execute(
        context,
        name="created",
        flow_data=sample_canonical_document,
    )

    async with workflow_save_db.session() as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT CAST(id AS TEXT) AS id,
                           CAST(user_id AS TEXT) AS user_id,
                           name
                    FROM flows
                    WHERE lower(replace(CAST(id AS TEXT), '-', '')) IN (:owned, :other, :created)
                    ORDER BY name
                    """
                ),
                {
                    "owned": str(owned_flow_id).replace("-", "").lower(),
                    "other": str(other_flow_id).replace("-", "").lower(),
                    "created": str(created.data["flow_id"]).replace("-", "").lower(),
                },
            )
        ).mappings().all()

    by_name = {row["name"]: row for row in rows}
    assert denied.success is False
    assert "access denied" in (denied.error or "")
    assert updated.success is True
    assert created.success is True
    assert by_name["other old"]["user_id"] == str(other_id)
    assert by_name["owned new"]["id"] == str(owned_flow_id)
    assert by_name["created"]["user_id"] == str(owner_id)
