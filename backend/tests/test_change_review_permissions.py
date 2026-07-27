"""Change-review owner and SQLite UUID compatibility regressions."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import text

from leagent.api.v1.chat import reviews as reviews_mod
from leagent.api.v1.chat.reviews import get_change_review_diff, reject_change_review
from leagent.config.settings import Settings
from leagent.db.service import DatabaseService


@pytest_asyncio.fixture()
async def review_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    db_path = tmp_path / "change-reviews.db"
    monkeypatch.setenv("DB_DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    db = DatabaseService(Settings())
    await db.create_tables()
    monkeypatch.setattr(reviews_mod, "_get_db", lambda: db)
    try:
        yield db
    finally:
        await db.dispose()


async def _insert_dashed_review(
    db: DatabaseService,
    *,
    review_id: UUID,
    session_id: UUID,
    user_id: UUID | None,
    status: str = "pending",
) -> None:
    now = datetime.utcnow().isoformat(sep=" ")
    async with db.session() as session:
        await session.execute(
            text(
                """
                INSERT INTO change_reviews (
                    created_at, updated_at, id, session_id, user_id,
                    workspace_mode, project_root, worktree_path, branch,
                    base_branch, title, files_changed, additions, deletions,
                    status
                )
                VALUES (
                    :now, :now, :id, :sid, :uid,
                    'worktree', :project_root, :worktree_path, 'review-branch',
                    'main', 'review', 1, 1, 0,
                    :status
                )
                """
            ),
            {
                "now": now,
                "id": str(review_id),
                "sid": str(session_id),
                "uid": str(user_id) if user_id is not None else None,
                "project_root": "D:/LeAgent",
                "worktree_path": "D:/LeAgent/.worktrees/review",
                "status": status,
            },
        )


@pytest.mark.asyncio
async def test_change_review_diff_denies_non_owner_before_worktree_access(
    review_db: DatabaseService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner_id = uuid4()
    other_id = uuid4()
    review_id = uuid4()
    await _insert_dashed_review(
        review_db,
        review_id=review_id,
        session_id=uuid4(),
        user_id=owner_id,
    )
    manager = SimpleNamespace(
        diff=lambda _info: pytest.fail("diff should not be called for non-owner"),
        changed_files=lambda _info: pytest.fail("changed_files should not be called for non-owner"),
    )
    monkeypatch.setattr(reviews_mod, "get_worktree_manager", lambda: manager)

    with pytest.raises(HTTPException) as exc:
        await get_change_review_diff(review_id, user_id=other_id)

    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_change_review_reject_updates_owned_legacy_dashed_review_id(
    review_db: DatabaseService,
) -> None:
    owner_id = uuid4()
    review_id = uuid4()
    await _insert_dashed_review(
        review_db,
        review_id=review_id,
        session_id=uuid4(),
        user_id=owner_id,
    )

    result = await reject_change_review(
        review_id,
        user_id=owner_id,
        body={"reason": "needs changes"},
    )

    async with review_db.session() as session:
        row = (
            await session.execute(
                text(
                    """
                    SELECT status, decided_by, reject_reason
                    FROM change_reviews
                    WHERE id = :review_id
                    """
                ),
                {"review_id": str(review_id)},
            )
        ).mappings().one()

    assert result["status"] == "rejected"
    assert row["status"] == "rejected"
    assert row["decided_by"] == str(owner_id)
    assert row["reject_reason"] == "needs changes"
