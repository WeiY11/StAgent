from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from leagent.api.v1 import chat as chat_api
from leagent.api.v1.chat.paths import (
    attachment_local_path_for_sse,
    dedupe_resolved_paths,
)
from leagent.file.attachment_context import (
    build_attachment_lookup,
    normalise_attachment_alias,
    normalize_attachment_paths,
)
from leagent.services.session.state import SessionAttachment


@pytest.mark.asyncio
async def test_resolve_request_attachment_paths_maps_id_and_name(monkeypatch, tmp_path: Path) -> None:
    session_id = uuid4()
    user_id = uuid4()
    attachment_id = uuid4()
    stored = tmp_path / f"{attachment_id}_budget.xlsx"
    stored.write_text("demo")

    att = SessionAttachment(
        id=attachment_id,
        session_id=session_id,
        filename="budget.xlsx",
        storage_path=str(stored),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        kind="document",
        size=4,
        sha256="abc123",
    )

    calls = []

    class _SessionManager:
        async def list_attachments(self, _sid, *, user_id=None):
            calls.append(user_id)
            return [att]

    fake_sm = SimpleNamespace(session_manager=_SessionManager())
    monkeypatch.setattr("leagent.main.get_service_manager", lambda: fake_sm)

    resolved = await chat_api._resolve_request_attachment_paths(
        session_id,
        [str(attachment_id), "budget.xlsx"],
        user_id=user_id,
    )
    assert str(stored.resolve()) in resolved
    assert all(Path(p).is_absolute() for p in resolved)
    assert calls == [user_id]


@pytest.mark.asyncio
async def test_resolve_request_attachment_paths_preserves_authorized_s3_uri(
    monkeypatch,
) -> None:
    """An authorised object-store reference must reach the turn materializer unchanged.

    The production change this catches is calling ``Path.resolve()`` on an
    ``s3://`` URI: that turns a durable object-store reference into a fake local
    path before the controller has a chance to download it safely.
    """
    session_id = uuid4()
    user_id = uuid4()
    attachment_id = uuid4()
    storage_path = f"s3://chat-files/{session_id}/{attachment_id}_photo.png"
    att = SessionAttachment(
        id=attachment_id,
        session_id=session_id,
        filename="photo.png",
        storage_path=storage_path,
        content_type="image/png",
        kind="image",
        size=4,
        sha256="abc123",
    )

    class _SessionManager:
        async def list_attachments(self, _sid, *, user_id=None):  # noqa: ANN001, ARG002
            return [att]

    monkeypatch.setattr(
        "leagent.main.get_service_manager",
        lambda: SimpleNamespace(session_manager=_SessionManager()),
    )

    resolved = await chat_api._resolve_request_attachment_paths(
        session_id,
        [str(attachment_id)],
        user_id=user_id,
    )

    assert resolved == [storage_path]


@pytest.mark.asyncio
async def test_resolve_request_attachment_paths_rejects_unknown_local_path(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """Client-supplied paths must not become implicit agent read grants."""
    session_id = uuid4()
    user_id = uuid4()
    unowned_file = tmp_path / "outside-session.txt"
    unowned_file.write_text("private")

    class _SessionManager:
        async def list_attachments(self, _sid, *, user_id=None):  # noqa: ARG002
            return []

    monkeypatch.setattr(
        "leagent.main.get_service_manager",
        lambda: SimpleNamespace(session_manager=_SessionManager()),
    )

    ignored = await chat_api._resolve_request_attachment_paths(
        session_id,
        [str(unowned_file)],
        user_id=user_id,
        reject_unknown=False,
    )
    assert ignored == []

    with pytest.raises(HTTPException) as exc_info:
        await chat_api._resolve_request_attachment_paths(
            session_id,
            [str(unowned_file)],
            user_id=user_id,
        )

    assert exc_info.value.status_code == 400


def test_normalize_attachment_paths_filters_and_dedupes(tmp_path: Path) -> None:
    f = tmp_path / "a.txt"
    f.write_text("x")

    normalized = normalize_attachment_paths(
        [
            "relative-id-like-value",
            str(f),
            str(f),
        ]
    )
    assert normalized == [str(f.resolve())]


def test_chat_path_helpers_never_turn_an_s3_uri_into_a_local_path() -> None:
    remote = "s3://chat-files/tenant/evidence.pdf"

    assert dedupe_resolved_paths([remote]) == []
    assert attachment_local_path_for_sse(remote) is None


def test_build_attachment_lookup_from_session_rows(tmp_path: Path) -> None:
    session_id = uuid4()
    attachment_id = uuid4()
    stored = tmp_path / f"{attachment_id}_budget_report.xlsx"
    stored.write_text("x")
    attachment = SessionAttachment(
        id=attachment_id,
        session_id=session_id,
        filename="Budget Report.xlsx",
        storage_path=str(stored),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        kind="document",
        size=1,
        sha256="abc",
    )

    lookup = build_attachment_lookup(
        session_attachments=[attachment],
        normalized_attachments=[str(stored.resolve())],
    )
    assert lookup["by_id"][str(attachment_id)] == str(stored.resolve())
    assert lookup["by_name"]["budgetreportxlsx"] == str(stored.resolve())
    assert lookup["by_name"]["budgetreport"] == str(stored.resolve())


def test_lookup_registers_uuid_from_knowledge_storage_basename(
    tmp_path: Path,
) -> None:
    """Merged knowledge paths use ``<file_uuid>_<name>`` on disk — expose *file_uuid* in by_id."""
    kid = uuid4()
    stored = tmp_path / f"{kid}_文章.doc"
    stored.write_text("x")
    lookup = build_attachment_lookup(
        session_attachments=[],
        normalized_attachments=[str(stored.resolve())],
    )
    assert lookup["by_id"][str(kid).lower()] == str(stored.resolve())


def test_lookup_maps_bare_original_name_for_uuid_prefixed_file(
    tmp_path: Path,
) -> None:
    """Bare ``工作簿1.csv`` must match ``<uuid>_工作簿1.csv`` via by_name (CJK-safe)."""
    kid = uuid4()
    stored = tmp_path / f"{kid}_工作簿1.csv"
    stored.write_text("x")
    lookup = build_attachment_lookup(
        session_attachments=[],
        normalized_attachments=[str(stored.resolve())],
    )
    key = normalise_attachment_alias("工作簿1.csv")
    assert key
    assert lookup["by_name"][key] == str(stored.resolve())
