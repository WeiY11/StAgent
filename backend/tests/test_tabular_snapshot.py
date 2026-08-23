"""Tests for the attachment tabular snapshot builder.

Covers the pure :func:`build_tabular_snapshot` helper (format detection,
size guards, encoding fallback, degradation) plus one integration
assertion that ``SessionManager.attach_files`` writes the snapshot into
``SessionAttachment.extra``.
"""

from __future__ import annotations

import io
from uuid import uuid4

import pandas as pd
import pytest
from fastapi import UploadFile
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

import StAgent.db.models  # noqa: F401 - ensure metadata is loaded
from StAgent.config.settings import get_settings
from StAgent.services.session.manager import SessionManager
from StAgent.services.session.tabular_snapshot import (
    _SKIP_PARSE_BYTES,
    build_tabular_snapshot,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

_CSV_BYTES = (
    b"name,age,city\n"
    b"alice,30,paris\n"
    b"bob,,london\n"
    b"carol,41,tokyo\n"
    b"dave,25,berlin\n"
    b"erin,33,madrid\n"
    b"frank,29,rome\n"
)


def _xlsx_bytes() -> bytes:
    buf = io.BytesIO()
    df = pd.DataFrame({"product": ["a", "b", "c"], "price": [1.5, 2.0, None]})
    df.to_excel(buf, index=False)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# build_tabular_snapshot (pure function)
# ---------------------------------------------------------------------------


class TestBuildTabularSnapshot:
    def test_csv_snapshot_structure(self) -> None:
        snap = build_tabular_snapshot(_CSV_BYTES, "people.csv", "text/csv")

        assert snap is not None
        assert [c["name"] for c in snap["columns"]] == ["name", "age", "city"]
        for col in snap["columns"]:
            assert set(col) == {"name", "dtype", "null_pct"}
        age = next(c for c in snap["columns"] if c["name"] == "age")
        assert age["null_pct"] == pytest.approx(100.0 / 6, abs=0.01)
        assert snap["row_count_estimate"] == 6
        assert len(snap["sample_rows"]) == 5
        assert snap["sample_rows"][0]["name"] == "alice"
        assert snap["truncated"] is False

    def test_xlsx_snapshot(self) -> None:
        snap = build_tabular_snapshot(
            _xlsx_bytes(),
            "products.xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

        assert snap is not None
        assert [c["name"] for c in snap["columns"]] == ["product", "price"]
        assert snap["row_count_estimate"] == 3
        assert len(snap["sample_rows"]) == 3
        # NaN cells must become JSON-safe None, not float("nan").
        assert snap["sample_rows"][2]["price"] is None
        assert snap["truncated"] is False

    def test_non_tabular_returns_none(self) -> None:
        assert build_tabular_snapshot(b"hello", "notes.txt", "text/plain") is None
        assert build_tabular_snapshot(b"\x89PNG", "img.png", "image/png") is None

    def test_corrupt_excel_returns_none(self) -> None:
        snap = build_tabular_snapshot(
            b"definitely not a zip archive",
            "broken.xlsx",
            "application/vnd.ms-excel",
        )
        assert snap is None

    def test_oversized_file_skips_parsing(self) -> None:
        class _FakeBytes(bytes):
            """Reports a >50MB length without allocating it."""

            def __len__(self) -> int:
                return _SKIP_PARSE_BYTES + 1

        snap = build_tabular_snapshot(_FakeBytes(b"a,b\n1,2\n"), "big.csv", "text/csv")

        assert snap == {
            "file_size": _SKIP_PARSE_BYTES + 1,
            "truncated": True,
            "hint": "large_file",
        }

    def test_gbk_encoded_csv_falls_back(self) -> None:
        raw = "姓名,年龄\n张三,30\n李四,25\n".encode("gbk")
        snap = build_tabular_snapshot(raw, "gbk.csv", "text/csv")

        assert snap is not None
        assert [c["name"] for c in snap["columns"]] == ["姓名", "年龄"]
        assert snap["sample_rows"][0]["姓名"] == "张三"

    def test_mime_only_detection(self) -> None:
        snap = build_tabular_snapshot(_CSV_BYTES, "export", "text/csv")
        assert snap is not None
        assert snap["row_count_estimate"] == 6


# ---------------------------------------------------------------------------
# SessionManager integration
# ---------------------------------------------------------------------------


class _InMemoryDatabase:
    """Minimal stand-in for :class:`DatabaseService` backed by aiosqlite."""

    def __init__(self) -> None:
        self._engine = create_async_engine(
            "sqlite+aiosqlite:///:memory:", echo=False, future=True
        )
        self._session_factory = sessionmaker(
            self._engine, class_=AsyncSession, expire_on_commit=False
        )

    async def start(self) -> None:
        async with self._engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)

    async def dispose(self) -> None:
        await self._engine.dispose()

    def session(self):  # noqa: ANN201 - async context manager
        return _SessionContext(self._session_factory)


class _SessionContext:
    def __init__(self, factory) -> None:  # noqa: ANN001
        self._factory = factory
        self._session: AsyncSession | None = None

    async def __aenter__(self) -> AsyncSession:
        self._session = self._factory()
        return self._session

    async def __aexit__(self, exc_type, exc, tb) -> None:
        assert self._session is not None
        try:
            if exc_type is None:
                await self._session.commit()
            else:
                await self._session.rollback()
        finally:
            await self._session.close()


@pytest.fixture()
def settings_for_session(tmp_path):
    s = get_settings()
    s.files.upload_dir = str(tmp_path / "uploads")
    s.files.max_upload_bytes = 1024 * 1024
    s.files.preview_ttl_seconds = 60
    s.files.signed_url_secret = "unit-test-signing-secret"
    s.session.in_memory_lru_size = 4
    return s


@pytest.fixture()
async def database() -> _InMemoryDatabase:
    db = _InMemoryDatabase()
    await db.start()
    yield db
    await db.dispose()


@pytest.fixture()
async def manager(settings_for_session, database) -> SessionManager:  # noqa: ANN001
    return SessionManager(settings_for_session, database=database)


@pytest.mark.asyncio
async def test_attach_files_stores_tabular_snapshot(
    manager: SessionManager,
) -> None:
    sid = uuid4()
    user_id = uuid4()
    await manager.get_or_create(sid, user_id=user_id)

    upload = UploadFile(
        file=io.BytesIO(_CSV_BYTES),
        filename="people.csv",
        headers={"content-type": "text/csv"},
    )
    attachments = await manager.attach_files(sid, [upload], user_id=user_id)

    assert len(attachments) == 1
    snap = attachments[0].extra.get("tabular_snapshot")
    assert snap is not None
    assert [c["name"] for c in snap["columns"]] == ["name", "age", "city"]
    assert snap["row_count_estimate"] == 6

    # The snapshot must survive the state round-trip too.
    state = await manager.load(sid)
    assert state is not None
    stored = state.attachments[0].extra.get("tabular_snapshot")
    assert stored is not None
    assert stored["row_count_estimate"] == 6


@pytest.mark.asyncio
async def test_attach_files_non_tabular_has_no_snapshot(
    manager: SessionManager,
) -> None:
    sid = uuid4()
    user_id = uuid4()
    await manager.get_or_create(sid, user_id=user_id)

    upload = UploadFile(
        file=io.BytesIO(b"plain text"),
        filename="notes.txt",
        headers={"content-type": "text/plain"},
    )
    attachments = await manager.attach_files(sid, [upload], user_id=user_id)

    assert len(attachments) == 1
    assert "tabular_snapshot" not in attachments[0].extra
