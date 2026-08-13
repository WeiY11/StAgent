"""Persistence tests for the mission repository (SQLite)."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio

from leagent.config.settings import Settings
from leagent.db.models.mission import Mission, MissionStatus, MissionStep
from leagent.db.service import DatabaseService


@pytest_asyncio.fixture()
async def repo_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    db_path = tmp_path / "missions.db"
    monkeypatch.setenv("DB_DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    db = DatabaseService(Settings())
    await db.create_tables()
    try:
        yield db
    finally:
        await db.dispose()


async def _insert_user(db: DatabaseService) -> UUID:
    from leagent.db.models.identity_stub import UserStub

    user_id = uuid4()
    async with db.session() as session:
        # Insert via the ORM so the id is stored the same way (hex on
        # SQLite) as the ORM-written ``agent_missions.user_id`` FK value.
        session.add(UserStub(id=user_id, username=f"u-{user_id.hex[:8]}"))
    return user_id


def _make_step(mission_id: UUID, step_no: int, *, plan_id: int = 0) -> MissionStep:
    return MissionStep(
        mission_id=mission_id,
        plan_id=plan_id,
        step_no=step_no,
        description=f"step {step_no}",
    )


async def test_create_and_get(repo_db):
    repo = repo_db.repositories.missions
    mission = await repo.create(Mission(goal="写一个 CLI 工具"))
    fetched = await repo.get(mission.id)
    assert fetched is not None
    assert fetched.goal == "写一个 CLI 工具"
    assert fetched.status == MissionStatus.PLANNING


async def test_upsert_step_is_idempotent(repo_db):
    repo = repo_db.repositories.missions
    mission = await repo.create(Mission(goal="g"))
    step = _make_step(mission.id, 1)
    first = await repo.upsert_step(step)
    step.description = "updated"
    await repo.upsert_step(step)
    steps = await repo.list_steps(mission.id)
    assert len(steps) == 1
    assert steps[0].id == first.id
    assert steps[0].description == "updated"


async def test_get_with_steps_orders_by_step_no(repo_db):
    repo = repo_db.repositories.missions
    mission = await repo.create(Mission(goal="g"))
    await repo.replace_steps(mission.id, 0, [_make_step(mission.id, 2), _make_step(mission.id, 1)])
    got = await repo.get_with_steps(mission.id)
    assert got is not None
    _mission, steps = got
    assert [s.step_no for s in steps] == [1, 2]


async def test_replace_steps_scoped_to_plan(repo_db):
    repo = repo_db.repositories.missions
    mission = await repo.create(Mission(goal="g"))
    await repo.replace_steps(mission.id, 0, [_make_step(mission.id, 1)])
    await repo.replace_steps(mission.id, 1, [_make_step(mission.id, 2, plan_id=1)])
    assert len(await repo.list_steps(mission.id)) == 2
    assert [s.step_no for s in await repo.list_steps(mission.id, plan_id=0)] == [1]
    assert [s.step_no for s in await repo.list_steps(mission.id, plan_id=1)] == [2]


async def test_update_status_and_list_for_user(repo_db):
    repo = repo_db.repositories.missions
    user_id = await _insert_user(repo_db)
    m1 = await repo.create(Mission(goal="a", user_id=user_id))
    m2 = await repo.create(Mission(goal="b", user_id=user_id))
    assert await repo.update_status(m2.id, MissionStatus.RUNNING)
    rows = await repo.list_for_user(user_id)
    assert {m.id for m in rows} == {m1.id, m2.id}
    running = await repo.list_for_user(user_id, status=MissionStatus.RUNNING)
    assert [m.id for m in running] == [m2.id]


async def test_delete_removes_steps(repo_db):
    repo = repo_db.repositories.missions
    mission = await repo.create(Mission(goal="g"))
    await repo.replace_steps(mission.id, 0, [_make_step(mission.id, 1)])
    await repo.delete(mission.id)
    assert await repo.get(mission.id) is None
    assert await repo.list_steps(mission.id) == []
