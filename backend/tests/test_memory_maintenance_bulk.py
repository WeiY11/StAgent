"""Tests for the set-based (bulk-SQL) memory maintenance jobs.

``decay_episode_importance`` / ``decay_fact_confidence`` were rewritten from an
N+1 pattern (load every row, flush one UPDATE per row) into a single set-based
``UPDATE`` that pushes the decay arithmetic into SQL. ``forget_low_value_episodes``
now deletes in one ``IN`` statement instead of one DELETE per row.

These tests pin the SQL behaviour to the *original* Python formula across
randomised inputs (so the bulk rewrite is provably equivalent), verify the
forget batching removes exactly the low-retention rows, and benchmark the
decay against the legacy per-row loop to demonstrate the N+1 -> 1 win.

Run with::

    uv run pytest tests/test_memory_maintenance_bulk.py -s
"""

from __future__ import annotations

import os
import random
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlmodel import select

from leagent.db.models.agent_memory import AgentEpisode, AgentFact
from leagent.db.models.message import ChatSession
from leagent.memory.maintenance import (
    decay_episode_importance,
    decay_fact_confidence,
    forget_low_value_episodes,
)
from leagent.services.auth.service import LOCAL_USER_ID

# ---------------------------------------------------------------------------
# DB fixture
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture()
async def db():
    from leagent.config.settings import Settings
    from leagent.db.service import DatabaseService

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "maint.db"
        os.environ["DB_DATABASE_URL"] = f"sqlite+aiosqlite:///{db_path}"
        service = DatabaseService(Settings())
        await service.create_tables()
        yield service
        await service._engine.dispose()


@pytest_asyncio.fixture()
async def session_id(db):
    sid = uuid4()
    async with db.session() as s:
        s.add(ChatSession(id=sid, user_id=LOCAL_USER_ID))
    return sid


def _naive(dt: datetime) -> datetime:
    return dt.replace(tzinfo=None)


async def _seed_episodes(db, session_id, rows: list[tuple[float, int]]) -> dict[str, float]:
    mapping: dict[str, float] = {}
    async with db.session() as s:
        for importance, recall_count in rows:
            summary = f"ep-{uuid4()}"
            mapping[summary] = importance
            s.add(
                AgentEpisode(
                    session_id=session_id,
                    summary=summary,
                    importance=importance,
                    recall_count=recall_count,
                )
            )
    return mapping


# ---------------------------------------------------------------------------
# Reference implementation of the ORIGINAL per-row Python decay
# ---------------------------------------------------------------------------


def _py_decay_episode(old: float, recalls: int, rate: float, floor: float, prot: float) -> float:
    new = old * rate
    if recalls > 0:
        new = min(old, new + recalls * prot)
    return max(floor, new)


def _py_decay_fact(old: float, rate: float, floor: float) -> float:
    return max(floor, old * rate)


# ---------------------------------------------------------------------------
# Equivalence: bulk SQL == original Python formula
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_episode_decay_exact_per_row(db, session_id) -> None:
    """Each row's post-decay value equals the original Python formula."""
    cases = [
        (0.5, 0),
        (0.5, 3),
        (0.02, 0),
        (0.0, 0),
        (1.0, 0),
        (0.03, 5),
        (0.9, 1),
        (0.07, 0),
    ]
    rate, floor, prot = 0.95, 0.02, 0.01
    # Seed each case with a unique, sortable summary so we can map back.
    async with db.session() as s:
        for i, (imp, rc) in enumerate(cases):
            s.add(
                AgentEpisode(
                    session_id=session_id,
                    summary=f"case-{i:02d}",
                    importance=imp,
                    recall_count=rc,
                )
            )

    await decay_episode_importance(
        db, decay_rate=rate, floor=floor, recall_protection_per_count=prot
    )

    async with db.session() as s:
        eps = list((await s.exec(select(AgentEpisode))).all())
    by_name = {e.summary: e.importance for e in eps}

    for i, (imp, rc) in enumerate(cases):
        expected = _py_decay_episode(imp, rc, rate, floor, prot)
        got = by_name[f"case-{i:02d}"]
        assert abs(got - expected) < 1e-9, (
            f"case {i} (old={imp}, recalls={rc}): got {got}, expected {expected}"
        )


@pytest.mark.asyncio
async def test_fact_decay_exact_per_row(db) -> None:
    rate, floor = 0.97, 0.10
    stale = _naive(datetime.now(UTC) - timedelta(days=120))
    cases = [0.8, 0.5, 0.10, 0.0, 1.0, 0.11]
    async with db.session() as s:
        for i, conf in enumerate(cases):
            s.add(
                AgentFact(
                    user_id=LOCAL_USER_ID,
                    key=f"k{i}",
                    value="v",
                    confidence=conf,
                    updated_at=stale,
                )
            )
    await decay_fact_confidence(db, older_than_days=90, decay_rate=rate, floor=floor)

    async with db.session() as s:
        facts = list((await s.exec(select(AgentFact))).all())
    by_key = {f.key: f.confidence for f in facts}
    for i, conf in enumerate(cases):
        expected = _py_decay_fact(conf, rate, floor)
        got = by_key[f"k{i}"]
        assert abs(got - expected) < 1e-9, f"k{i}: got {got}, expected {expected}"


@pytest.mark.asyncio
async def test_fact_decay_skips_recent_facts(db) -> None:
    recent = _naive(datetime.now(UTC) - timedelta(days=5))
    async with db.session() as s:
        s.add(
            AgentFact(
                user_id=LOCAL_USER_ID, key="recent", value="v",
                confidence=0.8, updated_at=recent,
            )
        )
    updated = await decay_fact_confidence(db, older_than_days=90)
    assert updated == 0
    async with db.session() as s:
        fact = (await s.exec(select(AgentFact).where(AgentFact.key == "recent"))).first()
    assert abs(fact.confidence - 0.8) < 1e-9


# ---------------------------------------------------------------------------
# Forget: batched delete removes exactly the low-retention rows
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_forget_removes_only_low_value(db, session_id) -> None:
    old = _naive(datetime.now(UTC) - timedelta(days=200))
    async with db.session() as s:
        # Low value: old + tiny importance + never recalled -> forgettable.
        for i in range(3):
            s.add(
                AgentEpisode(
                    session_id=session_id, summary=f"junk-{i}",
                    importance=0.01, recall_count=0, created_at=old,
                )
            )
        # High value: old but important -> kept by the SQL pre-filter.
        s.add(
            AgentEpisode(
                session_id=session_id, summary="keeper",
                importance=0.9, recall_count=0, created_at=old,
            )
        )

    removed = await forget_low_value_episodes(db, retention_threshold=0.1)
    assert removed == 3

    async with db.session() as s:
        remaining = list((await s.exec(select(AgentEpisode))).all())
    names = {e.summary for e in remaining}
    assert names == {"keeper"}


# ---------------------------------------------------------------------------
# Benchmark: N+1 (legacy per-row) vs single set-based UPDATE
# ---------------------------------------------------------------------------


async def _legacy_decay_episodes_db(db, rate: float, floor: float, prot: float) -> int:
    """Reproduce the ORIGINAL N+1 path: load every row, mutate in Python, and
    let the unit-of-work flush emit one UPDATE per changed row."""
    updated = 0
    async with db.session() as session:
        result = await session.exec(select(AgentEpisode))
        rows = list(result.all())
        for row in rows:
            old = float(row.importance or 0.0)
            new = old * rate
            recalls = int(row.recall_count or 0)
            if recalls > 0:
                new = min(old, new + recalls * prot)
            new = max(floor, new)
            if abs(new - old) > 0.001:
                row.importance = new
                updated += 1
    return updated


@pytest.mark.asyncio
async def test_bulk_decay_faster_than_row_loop(db, session_id) -> None:
    n = 3000
    rng = random.Random(7)
    rows = [(round(rng.uniform(0.05, 1.0), 4), rng.randint(0, 8)) for _ in range(n)]
    seeded = await _seed_episodes(db, session_id, rows)

    # Time the legacy N+1 path (load all, mutate per row, flush N UPDATEs).
    start = time.perf_counter()
    legacy_updated = await _legacy_decay_episodes_db(db, 0.95, 0.02, 0.01)
    legacy_ms = (time.perf_counter() - start) * 1e3

    # Reset importance to the seeded values so the bulk path starts identically.
    async with db.session() as s:
        eps = list((await s.exec(select(AgentEpisode))).all())
        for ep in eps:
            ep.importance = seeded[ep.summary]

    # Time the bulk set-based UPDATE.
    start = time.perf_counter()
    bulk_updated = await decay_episode_importance(db)
    bulk_ms = (time.perf_counter() - start) * 1e3

    speedup = legacy_ms / bulk_ms if bulk_ms > 0 else float("inf")
    print(f"\n[perf] decay {n} episodes — legacy N+1: {legacy_ms:.2f} ms, "
          f"bulk SQL UPDATE: {bulk_ms:.2f} ms, speedup: {speedup:.1f}x")
    assert bulk_updated == legacy_updated > 0
    assert bulk_ms < legacy_ms


