"""Tests for @knowledge references in chat messages."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy import text

from StAgent.api.v1 import chat as chat_api
from StAgent.config.settings import Settings
from StAgent.db.service import DatabaseService
from StAgent.file.service import FileService
from StAgent.file.storage.s3 import S3StorageBackend


def _db_returning_knowledge_row(row: object | None) -> MagicMock:
    """Build the narrow DB boundary used by knowledge-reference resolution."""
    result = MagicMock()
    result.first = MagicMock(return_value=row)

    session = MagicMock()
    session.exec = AsyncMock(return_value=result)

    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=None)

    db = MagicMock()
    db.session = MagicMock(return_value=cm)
    return db


class _S3Client:
    """In-memory object-store double for the FileService integration boundary."""

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], dict[str, Any]] = {}
        self.get_calls = 0

    def put_object(self, **kwargs: Any) -> None:
        self.objects[(kwargs["Bucket"], kwargs["Key"])] = {
            "body": bytes(kwargs["Body"]),
            "content_type": kwargs.get("ContentType", "application/octet-stream"),
        }

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        self.get_calls += 1
        item = self.objects[(kwargs["Bucket"], kwargs["Key"])]
        return {
            "Body": BytesIO(item["body"]),
            "ContentType": item["content_type"],
        }


async def _create_remote_folder_context(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> tuple[DatabaseService, FileService, _S3Client, object, object, object]:
    """Create one catalogued S3 file in an owner-scoped folder."""
    db_path = tmp_path / "folder-context-s3.db"
    monkeypatch.setenv("DB_DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    db = DatabaseService(Settings())
    await db.create_tables()
    user_id = uuid4()
    folder_id = uuid4()
    client = _S3Client()
    service = FileService(
        default_backend=S3StorageBackend(client, bucket="knowledge", prefix="tenant"),
        default_backend_name="s3",
    )
    ref = await service.register(
        b"folder-scoped remote evidence",
        filename="folder-evidence.txt",
        user_id=user_id,
        library_scope="knowledge",
        persist_db_row=False,
    )
    now = datetime.utcnow().isoformat(sep=" ")
    async with db.session() as session:
        await session.execute(
            text("INSERT INTO users (id, role, disabled) VALUES (:id, 'user', 0)"),
            {"id": str(user_id)},
        )
        await session.execute(
            text(
                "INSERT INTO folders "
                "(is_deleted, created_at, updated_at, id, name, user_id, parent_id, "
                "position, file_count, flow_count, is_project) "
                "VALUES (0, :now, :now, :id, :name, :user_id, NULL, 0, 1, 0, 0)"
            ),
            {
                "now": now,
                "id": str(folder_id),
                "name": "Remote evidence",
                "user_id": str(user_id),
            },
        )
        await session.execute(
            text(
                "INSERT INTO files "
                "(is_deleted, created_at, updated_at, id, name, original_name, file_type, "
                "mime_type, size, status, user_id, folder_id, storage_path, checksum, has_ocr, "
                "is_indexed, library_scope, inbox_state, is_pinned) "
                "VALUES (0, :now, :now, :id, :name, :name, 'document', 'text/plain', "
                ":size, 'processed', :user_id, :folder_id, :storage_path, :checksum, 0, 1, "
                "'knowledge', 'new', 0)"
            ),
            {
                "now": now,
                "id": str(ref.id),
                "name": ref.filename,
                "size": ref.size,
                "user_id": str(user_id),
                "folder_id": str(folder_id),
                "storage_path": ref.metadata["storage_path"],
                "checksum": ref.checksum,
            },
        )
    return db, service, client, user_id, folder_id, ref.id


def test_parse_knowledge_line_payload_uuid_suffix() -> None:
    uid = uuid4()
    fid, name = chat_api._parse_knowledge_line_payload(f"需求清单.docx#{uid}")
    assert fid == uid
    assert name == "需求清单.docx"


def test_parse_knowledge_line_payload_name_only() -> None:
    fid, name = chat_api._parse_knowledge_line_payload("doc only.docx")
    assert fid is None
    assert name == "doc only.docx"


def test_parse_knowledge_line_payload_hash_in_name() -> None:
    uid = uuid4()
    fid, name = chat_api._parse_knowledge_line_payload(f"part#v2.docx#{uid}")
    assert fid == uid
    assert name == "part#v2.docx"


@pytest.mark.asyncio
async def test_resolve_knowledge_message_paths_by_file_id(tmp_path: Path) -> None:
    user_id = uuid4()
    file_id = uuid4()
    p = tmp_path / "stored.txt"
    p.write_text("x")

    fake_row = MagicMock()
    fake_row.storage_path = str(p)

    fake_result = MagicMock()
    fake_result.first = MagicMock(return_value=fake_row)

    fake_sess = MagicMock()
    fake_sess.exec = AsyncMock(return_value=fake_result)

    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=fake_sess)
    cm.__aexit__ = AsyncMock(return_value=None)

    db = MagicMock()
    db.session = MagicMock(return_value=cm)

    msg = f"see @knowledge:ignored.txt#{file_id}"
    out = await chat_api._resolve_knowledge_message_paths(user_id, db, msg)
    assert len(out) == 1
    assert Path(out[0]) == p.resolve()


@pytest.mark.asyncio
async def test_s3_knowledge_reference_is_staged_for_the_turn_then_removed() -> None:
    """An authorised remote knowledge ref is readable only during its lease."""
    user_id = uuid4()
    file_id = uuid4()
    row = SimpleNamespace(
        id=file_id,
        name="evidence.pdf",
        original_name="evidence.pdf",
        storage_path="s3://knowledge-bucket/tenant/evidence.pdf",
        mime_type="application/pdf",
        size=15,
        checksum="abc123",
        user_id=user_id,
        session_id=None,
        library_scope="knowledge",
    )
    db = _db_returning_knowledge_row(row)

    class _FileService:
        def ref_from_persisted_record(self, **kwargs: object) -> object:
            assert kwargs["file_id"] == file_id
            assert kwargs["storage_path"] == row.storage_path
            return object()

        async def download(self, _ref: object) -> tuple[bytes, str]:
            return b"remote evidence", "application/pdf"

    factory = getattr(chat_api, "_materialize_knowledge_message_paths", None)
    assert factory is not None, "remote knowledge references need a turn-scoped materialization lease"

    async with factory(
        user_id,
        db,
        f"read @knowledge:evidence.pdf#{file_id}",
        file_service=_FileService(),
    ) as paths:
        assert len(paths) == 1
        staged = Path(paths[0])
        assert staged.suffix == ".pdf"
        assert staged.read_bytes() == b"remote evidence"

    assert not staged.exists()


@pytest.mark.asyncio
async def test_unauthorised_s3_knowledge_reference_never_downloads_or_stages() -> None:
    """A missing ACL match must be rejected before any object-store read."""
    user_id = uuid4()
    file_id = uuid4()
    db = _db_returning_knowledge_row(None)

    class _FileService:
        def ref_from_persisted_record(self, **_kwargs: object) -> object:
            raise AssertionError("unauthorised reference must not build a storage ref")

        async def download(self, _ref: object) -> tuple[bytes, str]:
            raise AssertionError("unauthorised reference must not download remote bytes")

    factory = getattr(chat_api, "_materialize_knowledge_message_paths", None)
    assert factory is not None, "remote knowledge references need a turn-scoped materialization lease"

    async with factory(
        user_id,
        db,
        f"read @knowledge:secret.pdf#{file_id}",
        file_service=_FileService(),
    ) as paths:
        assert paths == []


@pytest.mark.asyncio
async def test_s3_knowledge_acl_is_checked_before_file_service_download(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A real catalogue ACL gates S3 bytes, and the per-turn copy is removed."""
    db_path = tmp_path / "knowledge-s3.db"
    monkeypatch.setenv("DB_DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    db = DatabaseService(Settings())
    await db.create_tables()
    try:
        owner_id = uuid4()
        stranger_id = uuid4()
        client = _S3Client()
        service = FileService(
            default_backend=S3StorageBackend(client, bucket="knowledge", prefix="tenant"),
            default_backend_name="s3",
        )
        ref = await service.register(
            b"authorised remote evidence",
            filename="private-evidence.txt",
            user_id=owner_id,
            library_scope="knowledge",
            persist_db_row=False,
        )
        now = datetime.utcnow().isoformat(sep=" ")
        async with db.session() as session:
            await session.execute(
                text("INSERT INTO users (id, role, disabled) VALUES (:id, 'user', 0)"),
                {"id": str(owner_id)},
            )
            await session.execute(
                text(
                    "INSERT INTO files "
                    "(is_deleted, created_at, updated_at, id, name, original_name, file_type, "
                    "mime_type, size, status, user_id, storage_path, checksum, has_ocr, "
                    "is_indexed, library_scope, inbox_state, is_pinned) "
                    "VALUES (0, :now, :now, :id, :name, :name, 'document', 'text/plain', "
                    ":size, 'processed', :owner_id, :storage_path, :checksum, 0, 1, "
                    "'knowledge', 'new', 0)"
                ),
                {
                    "now": now,
                    "id": str(ref.id),
                    "name": ref.filename,
                    "size": ref.size,
                    "owner_id": str(owner_id),
                    "storage_path": ref.metadata["storage_path"],
                    "checksum": ref.checksum,
                },
            )

        message = f"read @knowledge:private-evidence.txt#{ref.id}"
        async with chat_api._materialize_knowledge_message_paths(
            owner_id,
            db,
            message,
            file_service=service,
        ) as paths:
            assert len(paths) == 1
            staged = Path(paths[0])
            assert staged.read_bytes() == b"authorised remote evidence"
        assert not staged.exists()
        assert client.get_calls == 1

        client.get_calls = 0
        async with chat_api._materialize_knowledge_message_paths(
            stranger_id,
            db,
            message,
            file_service=service,
        ) as paths:
            assert paths == []
        assert client.get_calls == 0
    finally:
        await db.dispose()


@pytest.mark.asyncio
async def test_s3_folder_context_stays_staged_until_sse_stream_closes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Closing a folder-context stream removes its authorised S3 staging file."""
    db, service, client, owner_id, folder_id, _file_id = await _create_remote_folder_context(
        monkeypatch,
        tmp_path,
    )
    staged: Path | None = None
    try:

        async def _sse_stream():
            nonlocal staged
            async with chat_api._materialize_folder_context_paths(
                owner_id,
                db,
                folder_id=str(folder_id),
                file_service=service,
            ) as paths:
                assert len(paths) == 1
                staged = Path(paths[0])
                assert staged.read_bytes() == b"folder-scoped remote evidence"
                yield "first SSE event"
                await asyncio.Event().wait()

        stream = _sse_stream()
        assert await stream.__anext__() == "first SSE event"
        assert staged is not None and staged.exists()
        await stream.aclose()
        assert staged is not None and not staged.exists()
        assert client.get_calls == 1

        client.get_calls = 0
        async with chat_api._materialize_folder_context_paths(
            uuid4(),
            db,
            folder_id=str(folder_id),
            file_service=service,
        ) as paths:
            assert paths == []
        assert client.get_calls == 0
    finally:
        await db.dispose()


@pytest.mark.asyncio
async def test_s3_explicit_file_context_removes_staging_after_agent_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An explicit file-id lease cleans up if the consuming agent stream fails."""
    db, service, client, owner_id, _folder_id, file_id = await _create_remote_folder_context(
        monkeypatch,
        tmp_path,
    )
    staged: Path | None = None
    try:
        with pytest.raises(RuntimeError, match="agent stream exploded"):
            async with chat_api._materialize_folder_context_paths(
                owner_id,
                db,
                file_ids_csv=str(file_id),
                file_service=service,
            ) as paths:
                assert len(paths) == 1
                staged = Path(paths[0])
                assert staged.read_bytes() == b"folder-scoped remote evidence"
                raise RuntimeError("agent stream exploded")

        assert staged is not None and not staged.exists()
        assert client.get_calls == 1

        client.get_calls = 0
        async with chat_api._materialize_folder_context_paths(
            uuid4(),
            db,
            file_ids_csv=str(file_id),
            file_service=service,
        ) as paths:
            assert paths == []
        assert client.get_calls == 0
    finally:
        await db.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("shutdown", ["aclose", "cancel"])
async def test_native_stream_closes_folder_and_knowledge_leases_on_client_shutdown(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    shutdown: str,
) -> None:
    """The native SSE iterator owns both remote-context leases through shutdown."""
    user_id = uuid4()
    session_id = uuid4()
    folder_id = uuid4()
    folder_stage = tmp_path / "folder-stage.txt"
    knowledge_stage = tmp_path / "knowledge-stage.txt"
    lease_state = {
        "folder_entered": False,
        "folder_exited": False,
        "knowledge_entered": False,
        "knowledge_exited": False,
    }
    seen_attachments: list[str] | None = None
    agent_waiting = asyncio.Event()

    @asynccontextmanager
    async def _folder_lease(*_args: object, **_kwargs: object):
        folder_stage.write_bytes(b"folder")
        lease_state["folder_entered"] = True
        try:
            yield [str(folder_stage)]
        finally:
            lease_state["folder_exited"] = True
            folder_stage.unlink(missing_ok=True)

    @asynccontextmanager
    async def _knowledge_lease(*_args: object, **_kwargs: object):
        knowledge_stage.write_bytes(b"knowledge")
        lease_state["knowledge_entered"] = True
        try:
            yield [str(knowledge_stage)]
        finally:
            lease_state["knowledge_exited"] = True
            knowledge_stage.unlink(missing_ok=True)

    async def _allow_access(*_args: object, **_kwargs: object) -> None:
        return None

    async def _agent_stream(*_args: object, **kwargs: object):
        nonlocal seen_attachments
        raw = kwargs.get("attachments")
        seen_attachments = list(raw) if isinstance(raw, list) else None
        assert folder_stage.exists()
        assert knowledge_stage.exists()
        yield "token", {"token": "partial"}, "partial"
        agent_waiting.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(chat_api, "_require_project_access", _allow_access)
    monkeypatch.setattr(chat_api, "_require_session_project_access", _allow_access)
    monkeypatch.setattr(
        "StAgent.services.chat.project_files.resolve_session_project_file_space",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        chat_api,
        "_resolve_folder_context",
        AsyncMock(return_value=[("s3://knowledge/folder.txt", "folder.txt", None)]),
    )
    monkeypatch.setattr(
        chat_api,
        "_resolve_project_folder_path",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        chat_api,
        "_resolve_folder_context_note",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(chat_api, "_materialize_folder_context_paths", _folder_lease)
    monkeypatch.setattr(chat_api, "_materialize_knowledge_message_paths", _knowledge_lease)
    monkeypatch.setattr(chat_api, "build_agent_controller", lambda: object())
    monkeypatch.setattr(
        "StAgent.services.chat.pet_personality.apply_pet_personality_to_agent",
        AsyncMock(),
    )
    monkeypatch.setattr(
        chat_api,
        "_authorized_root_paths_for_session",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(chat_api, "_run_agent_stream", _agent_stream)

    chat_service = SimpleNamespace(
        get_session=AsyncMock(return_value=SimpleNamespace(id=session_id, session_metadata=None)),
        add_message=AsyncMock(return_value=SimpleNamespace(id=uuid4())),
    )
    response = await chat_api.chat_stream_endpoint(
        user_id,
        chat_service,
        MagicMock(),
        message="read @knowledge:remote",
        session_id=str(session_id),
        files=[],
        history=None,
        project_id=None,
        project_unlock_token=None,
        folder_id=str(folder_id),
        file_ids=None,
        tool_replies=None,
        project_folder_id=None,
        runtime_profile=None,
        model_mode=None,
        model_provider=None,
        model_name=None,
        research_mode=None,
        research_doc=None,
        checkpoint_id=None,
    )
    stream = response.body_iterator
    await stream.__anext__()  # stream_start
    await stream.__anext__()  # agent_task
    await stream.__anext__()  # first token: both leases are active

    assert lease_state == {
        "folder_entered": True,
        "folder_exited": False,
        "knowledge_entered": True,
        "knowledge_exited": False,
    }
    assert seen_attachments is not None
    assert {Path(path).resolve() for path in seen_attachments} == {
        folder_stage.resolve(),
        knowledge_stage.resolve(),
    }

    if shutdown == "aclose":
        await stream.aclose()
    else:
        next_event = asyncio.create_task(stream.__anext__())
        await agent_waiting.wait()
        next_event.cancel()
        with pytest.raises(asyncio.CancelledError):
            await next_event

    assert lease_state == {
        "folder_entered": True,
        "folder_exited": True,
        "knowledge_entered": True,
        "knowledge_exited": True,
    }
    assert not folder_stage.exists()
    assert not knowledge_stage.exists()


@pytest.mark.asyncio
async def test_resolve_knowledge_message_paths_by_original_name(tmp_path: Path) -> None:
    user_id = uuid4()
    p = tmp_path / "b.txt"
    p.write_text("y")

    fake_row = MagicMock()
    fake_row.storage_path = str(p)

    fake_result = MagicMock()
    fake_result.first = MagicMock(return_value=None)
    fake_result.all = MagicMock(return_value=[fake_row])

    fake_sess = MagicMock()
    fake_sess.exec = AsyncMock(return_value=fake_result)

    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=fake_sess)
    cm.__aexit__ = AsyncMock(return_value=None)

    db = MagicMock()
    db.session = MagicMock(return_value=cm)

    out = await chat_api._resolve_knowledge_message_paths(
        user_id,
        db,
        "read @knowledge:UniqueName.docx please",
    )
    assert len(out) == 1
    assert Path(out[0]) == p.resolve()


def test_merge_agent_attachment_paths_dedupes() -> None:
    a = str(Path("/tmp/a.txt"))
    b = str(Path("/tmp/b.txt"))
    m = chat_api._merge_agent_attachment_paths([a], [a, b])
    assert m is not None
    assert len(m) == 2


@pytest.mark.asyncio
async def test_resolve_folder_context_file_ids_returns_paths(tmp_path: Path) -> None:
    """Explicit ``file_ids`` on chat requests must resolve to storage paths."""
    user_id = uuid4()
    file_id = uuid4()
    p = tmp_path / "kb.csv"
    p.write_text("a\n1")

    fake_row = MagicMock()
    fake_row.storage_path = str(p)
    fake_row.original_name = "kb.csv"
    fake_row.extracted_text = None

    fake_result = MagicMock()
    fake_result.all = MagicMock(return_value=[fake_row])

    fake_sess = MagicMock()
    fake_sess.exec = AsyncMock(return_value=fake_result)

    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=fake_sess)
    cm.__aexit__ = AsyncMock(return_value=None)

    db = MagicMock()
    db.session = MagicMock(return_value=cm)

    out = await chat_api._resolve_folder_context(
        user_id,
        db,
        folder_id=None,
        file_ids_csv=str(file_id),
    )
    assert len(out) == 1
    assert Path(out[0][0]).resolve() == p.resolve()
    assert out[0][1] == "kb.csv"
