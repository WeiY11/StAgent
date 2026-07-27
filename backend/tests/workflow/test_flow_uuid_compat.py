"""SQLite dashed/hex UUID compatibility for workflow flow-loading paths."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from leagent.config.settings import Settings
from leagent.db import get_database_service
from leagent.db.service import DatabaseService
from leagent.services.auth.deps import get_current_user_id
from leagent.workflow.registry import FlowWorkflowRegistry
from leagent.workflow.server.router import router as workflow_router
from leagent.workflow.worker import make_db_flow_loader


@pytest_asyncio.fixture()
async def flow_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    db_path = tmp_path / "flows.db"
    monkeypatch.setenv("DB_DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    db = DatabaseService(Settings())
    await db.create_tables()
    try:
        yield db
    finally:
        await db.dispose()


async def _insert_dashed_flow(
    db: DatabaseService,
    *,
    flow_id: UUID,
    user_id: UUID,
    document: dict,
    name: str = "Dashed Flow",
    folder_id: UUID | None = None,
) -> None:
    now = datetime.utcnow().isoformat(sep=" ")
    async with db.session() as session:
        await session.execute(
            text("INSERT INTO users (id, role, disabled) VALUES (:id, 'user', 0)"),
            {"id": str(user_id)},
        )
        await session.execute(
            text(
                "INSERT INTO flows "
                "(is_deleted, created_at, updated_at, id, name, status, flow_type, "
                "is_public, data, version, user_id, folder_id, run_count) "
                "VALUES (0, :now, :now, :id, :name, 'draft', 'workflow', 0, "
                ":data, 1, :uid, :folder_id, 0)"
            ),
            {
                "now": now,
                "id": str(flow_id),
                "name": name,
                "data": json.dumps(document),
                "uid": str(user_id),
                "folder_id": str(folder_id) if folder_id is not None else None,
            },
        )


async def _flow_client(db: DatabaseService, user_id: UUID) -> AsyncClient:
    app = FastAPI()
    app.include_router(workflow_router, prefix="/api/v1")

    async def _fake_user() -> UUID:
        return user_id

    app.dependency_overrides[get_current_user_id] = _fake_user
    app.dependency_overrides[get_database_service] = lambda: db
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_registry_and_worker_loader_match_dashed_sqlite_flow_ids(
    flow_db: DatabaseService,
    sample_canonical_document: dict,
) -> None:
    flow_id = uuid4()
    user_id = uuid4()
    await _insert_dashed_flow(
        flow_db,
        flow_id=flow_id,
        user_id=user_id,
        document=sample_canonical_document,
    )

    registry = FlowWorkflowRegistry(flow_db)
    worker_loader = make_db_flow_loader(flow_db)

    registry_doc = await registry.get(str(flow_id))
    worker_doc = await worker_loader(str(flow_id))

    assert registry_doc is not None
    assert registry_doc.id == sample_canonical_document["id"]
    assert worker_doc is not None
    assert worker_doc.id == sample_canonical_document["id"]


@pytest.mark.asyncio
async def test_workflow_http_flow_routes_match_and_update_dashed_sqlite_ids(
    flow_db: DatabaseService,
    sample_canonical_document: dict,
) -> None:
    from leagent.workflow.nodes import bootstrap

    await bootstrap()
    flow_id = uuid4()
    user_id = uuid4()
    await _insert_dashed_flow(
        flow_db,
        flow_id=flow_id,
        user_id=user_id,
        document=sample_canonical_document,
    )

    client = await _flow_client(flow_db, user_id)
    async with client:
        fetched = await client.get(f"/api/v1/workflow/flows/{flow_id}")
        validated = await client.get(f"/api/v1/workflow/flows/{flow_id}/validate")
        exported = await client.get(f"/api/v1/workflow/flows/{flow_id}/export")
        updated = await client.patch(
            f"/api/v1/workflow/flows/{flow_id}",
            json={"name": "Updated Dashed Flow"},
        )
        deleted = await client.delete(f"/api/v1/workflow/flows/{flow_id}")
        after_delete = await client.get(f"/api/v1/workflow/flows/{flow_id}")

    async with flow_db.session() as session:
        row = (
            await session.execute(
                text("SELECT name, is_deleted FROM flows WHERE id = :id"),
                {"id": str(flow_id)},
            )
        ).one()

    assert fetched.status_code == 200
    assert fetched.json()["id"] == str(flow_id)
    assert validated.status_code == 200
    assert validated.json()["ok"] is True
    assert exported.status_code == 200
    assert exported.json()["flow_id"] == str(flow_id)
    assert updated.status_code == 200
    assert updated.json()["name"] == "Updated Dashed Flow"
    assert deleted.status_code == 204
    assert after_delete.status_code == 404
    assert row._mapping["name"] == "Updated Dashed Flow"
    assert bool(row._mapping["is_deleted"]) is True
