"""Object-storage session-attachment leases across an agent turn."""

from __future__ import annotations

import asyncio
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from StAgent.services.session.state import SessionAttachment


class _InMemoryS3Client:
    """Tiny boto3-compatible object-store double for a FileService boundary test."""

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], tuple[bytes, str]] = {}
        self.get_calls = 0

    def put_object(self, **kwargs: Any) -> None:
        self.objects[(kwargs["Bucket"], kwargs["Key"])] = (
            bytes(kwargs["Body"]),
            str(kwargs.get("ContentType") or "application/octet-stream"),
        )

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        self.get_calls += 1
        body, content_type = self.objects[(kwargs["Bucket"], kwargs["Key"])]
        return {"Body": BytesIO(body), "ContentType": content_type}


class _RemoteFileService:
    """Narrow FileService double that records authorised object-store reads."""

    def __init__(self, body: bytes = b"image-bytes") -> None:
        self.body = body
        self.ref_requests: list[dict[str, Any]] = []
        self.download_calls = 0

    def ref_from_persisted_record(self, **kwargs: Any) -> object:
        self.ref_requests.append(kwargs)
        return SimpleNamespace(backend_name="s3")

    def get_backend(self, backend_name: str) -> Any:
        assert backend_name == "s3"
        return SimpleNamespace(
            key_from_storage_path=lambda path: "attachment-key" if path.startswith("s3://") else None,
        )

    async def download(self, _ref: object) -> tuple[bytes, str]:
        self.download_calls += 1
        return self.body, "image/png"


class _SessionManager:
    def __init__(self, attachments: list[Any], file_service: Any) -> None:
        self.attachments = attachments
        self._file_service = file_service
        self.calls: list[tuple[UUID, UUID | None]] = []

    async def list_attachments(
        self,
        session_id: UUID,
        *,
        user_id: UUID | None = None,
    ) -> list[Any]:
        self.calls.append((session_id, user_id))
        return list(self.attachments)


def _s3_attachment(session_id: UUID, attachment_id: UUID) -> SessionAttachment:
    return SessionAttachment(
        id=attachment_id,
        session_id=session_id,
        filename="photo.png",
        storage_path=f"s3://chat-files/{session_id}/{attachment_id}_photo.png",
        content_type="image/png",
        kind="image",
        size=11,
        sha256="sum",
    )


@pytest.mark.asyncio
async def test_materializes_authorized_s3_attachment_for_lease_then_removes_it() -> None:
    """A current-session S3 attachment is local only while its lease is active."""
    from StAgent.file.attachment_context import materialize_session_attachment_paths

    session_id = uuid4()
    user_id = uuid4()
    attachment = _s3_attachment(session_id, uuid4())
    file_service = _RemoteFileService()
    manager = _SessionManager([attachment], file_service)

    async with materialize_session_attachment_paths(
        manager,
        session_id,
        user_id=user_id,
    ) as lease:
        assert manager.calls == [(session_id, user_id)]
        assert file_service.download_calls == 1
        assert file_service.ref_requests[0]["file_id"] == attachment.id
        assert file_service.ref_requests[0]["session_id"] == session_id
        assert file_service.ref_requests[0]["user_id"] == user_id
        assert lease.paths and len(lease.paths) == 1
        staged = Path(lease.paths[0])
        assert staged.is_file()
        assert staged.read_bytes() == b"image-bytes"
        assert lease.session_attachments[0].storage_path == str(staged)
        assert lease.storage_path_aliases[attachment.storage_path] == str(staged)

    assert not staged.exists()


@pytest.mark.asyncio
async def test_materializer_downloads_session_object_through_real_file_service() -> None:
    """The lease reconstructs the configured S3 FileRef rather than a local URI path."""
    from StAgent.file.attachment_context import materialize_session_attachment_paths
    from StAgent.file.service import FileService
    from StAgent.file.storage.s3 import S3StorageBackend

    session_id = uuid4()
    user_id = uuid4()
    client = _InMemoryS3Client()
    service = FileService(
        default_backend=S3StorageBackend(client, bucket="chat-files", prefix="tenant"),
        default_backend_name="s3",
    )
    ref = await service.register(
        b"remote session evidence",
        filename="evidence.txt",
        user_id=user_id,
        session_id=session_id,
        persist_db_row=False,
    )
    attachment = SessionAttachment(
        id=ref.id,
        session_id=session_id,
        filename=ref.filename,
        storage_path=str(ref.metadata["storage_path"]),
        content_type=ref.content_type,
        kind="text",
        size=ref.size,
        sha256=ref.checksum,
    )
    manager = _SessionManager([attachment], service)

    async with materialize_session_attachment_paths(
        manager,
        session_id,
        user_id=user_id,
    ) as lease:
        staged = Path(lease.paths[0])
        assert staged.read_bytes() == b"remote session evidence"
        assert lease.storage_path_aliases[attachment.storage_path] == str(staged)
        assert client.get_calls == 1

    assert not staged.exists()


@pytest.mark.asyncio
async def test_materializer_preserves_separately_leased_s3_extra_path_without_downloading() -> None:
    """An unlisted URI remains available to its owning lease but is not downloaded here."""
    from StAgent.file.attachment_context import materialize_session_attachment_paths

    file_service = _RemoteFileService()
    manager = _SessionManager([], file_service)
    unlisted_uri = "s3://chat-files/private/secret.pdf"

    async with materialize_session_attachment_paths(
        manager,
        uuid4(),
        user_id=uuid4(),
        extra_paths=[unlisted_uri],
    ) as lease:
        assert lease.paths == [unlisted_uri]
        assert lease.session_attachments == []

    assert file_service.download_calls == 0


@pytest.mark.asyncio
async def test_materializer_fails_closed_when_authorized_s3_download_fails() -> None:
    """An unreadable authorised object must not leave behind a fake local grant."""
    from StAgent.file.attachment_context import materialize_session_attachment_paths

    class _FailingFileService:
        def ref_from_persisted_record(self, **_kwargs: object) -> object:
            return object()

        async def download(self, _ref: object) -> tuple[bytes, str]:
            raise OSError("object store unavailable")

    session_id = uuid4()
    attachment = _s3_attachment(session_id, uuid4())
    manager = _SessionManager([attachment], _FailingFileService())

    async with materialize_session_attachment_paths(
        manager,
        session_id,
        user_id=uuid4(),
    ) as lease:
        assert lease.paths == []
        assert lease.session_attachments == []
        assert lease.storage_path_aliases == {}


@pytest.mark.asyncio
async def test_materializer_never_uses_a_local_backend_as_s3_fallback() -> None:
    """A default local backend must not receive an unresolved S3 URI."""
    from StAgent.file.attachment_context import materialize_session_attachment_paths

    class _LocalOnlyFileService:
        def __init__(self) -> None:
            self.download_calls = 0

        def ref_from_persisted_record(self, **_kwargs: object) -> object:
            return SimpleNamespace(backend_name="local")

        def get_backend(self, backend_name: str) -> Any:
            assert backend_name == "local"
            return SimpleNamespace(key_from_storage_path=lambda _path: None)

        async def download(self, _ref: object) -> tuple[bytes, str]:
            self.download_calls += 1
            return b"must-not-read", "application/octet-stream"

    session_id = uuid4()
    attachment = _s3_attachment(session_id, uuid4())
    file_service = _LocalOnlyFileService()
    manager = _SessionManager([attachment], file_service)

    async with materialize_session_attachment_paths(
        manager,
        session_id,
        user_id=uuid4(),
    ) as lease:
        assert lease.paths == []

    assert file_service.download_calls == 0


@pytest.mark.asyncio
async def test_materializer_rejects_a_row_without_the_current_session_identity() -> None:
    """A malformed storage row cannot gain object-store read authority."""
    from StAgent.file.attachment_context import materialize_session_attachment_paths

    session_id = uuid4()
    attachment = SimpleNamespace(
        id=uuid4(),
        session_id=None,
        filename="orphan.png",
        storage_path="s3://chat-files/private/orphan.png",
        content_type="image/png",
        kind="image",
        size=1,
        sha256="sum",
    )
    file_service = _RemoteFileService()
    manager = _SessionManager([attachment], file_service)

    async with materialize_session_attachment_paths(
        manager,
        session_id,
        user_id=uuid4(),
    ) as lease:
        assert lease.paths == []
        assert lease.session_attachments == []

    assert file_service.download_calls == 0


@pytest.mark.asyncio
async def test_materializer_keeps_local_session_attachment_as_its_resolved_path(
    tmp_path: Path,
) -> None:
    """The object-store lease must not change normal local attachment behavior."""
    from StAgent.file.attachment_context import materialize_session_attachment_paths

    session_id = uuid4()
    local_file = tmp_path / "local.txt"
    local_file.write_text("local source", encoding="utf-8")
    attachment = SessionAttachment(
        id=uuid4(),
        session_id=session_id,
        filename="local.txt",
        storage_path=str(local_file),
        content_type="text/plain",
        kind="text",
        size=12,
        sha256="sum",
    )
    file_service = _RemoteFileService()
    manager = _SessionManager([attachment], file_service)

    async with materialize_session_attachment_paths(
        manager,
        session_id,
        user_id=uuid4(),
    ) as lease:
        assert lease.paths == [str(local_file.resolve())]
        assert lease.session_attachments[0].storage_path == str(local_file.resolve())
        assert local_file.read_text(encoding="utf-8") == "local source"

    assert local_file.exists()
    assert file_service.download_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["complete", "error", "cancel"])
async def test_controller_keeps_s3_image_staged_through_multimodal_turn_then_cleans(
    outcome: str,
) -> None:
    """Controller owns the lease through multimodal preparation and terminal cleanup.

    The regression would fail if the controller formatted a user message from
    the raw URI, reloaded original attachment rows after staging, or released
    the staged file before the engine consumes its image input.
    """
    from StAgent.agent.base import AgentConfig, ConversationContext
    from StAgent.agent.controller import AgentController
    from StAgent.agent.query_engine import SDKMessage
    from StAgent.file.attachment_context import normalise_attachment_alias

    session_id = uuid4()
    user_id = uuid4()
    attachment = _s3_attachment(session_id, uuid4())
    file_service = _RemoteFileService(b"png-source")
    manager = _SessionManager([attachment], file_service)

    controller = AgentController(
        llm=SimpleNamespace(model_registry=None),
        tools=MagicMock(),
        planner=MagicMock(),
        executor=SimpleNamespace(service_manager=SimpleNamespace(file_service=file_service)),
        session_manager=manager,
        config=AgentConfig(enable_memory=False, enable_streaming=False),
    )
    observed: dict[str, Any] = {}

    async def _fake_load(sid: UUID) -> ConversationContext:
        return ConversationContext(session_id=sid)

    async def _no_playbooks(_sid: UUID) -> list[str]:
        return []

    controller._load_conversation = _fake_load  # type: ignore[assignment]
    controller._save_conversation = AsyncMock()  # type: ignore[assignment]
    controller._record_episode = AsyncMock()  # type: ignore[assignment]
    controller._playbook_ids_for_session = _no_playbooks  # type: ignore[assignment]

    class _Engine:
        def __init__(self, tool_extra: dict[str, Any]) -> None:
            self.config = SimpleNamespace(
                session_id=session_id,
                user_id=user_id,
                agent_id="default_agent",
            )
            self.abort_event = asyncio.Event()
            self.mutable_messages: list[dict[str, Any]] = []
            self._tool_extra = tool_extra

        def abort(self) -> None:
            self.abort_event.set()

        async def submit_message(self, prompt: Any, **_kwargs: Any):
            staged = Path(self._tool_extra["attachments"][0])
            observed["staged"] = staged
            observed["exists_during_engine"] = staged.is_file()
            observed["prompt"] = prompt
            prompt_attachments = await self.config.session_manager.list_attachments(
                session_id,
                user_id=user_id,
            )
            observed["context_attachment_path"] = prompt_attachments[0].storage_path
            assert staged.read_bytes() == b"png-source"
            assert Path(prompt_attachments[0].storage_path).read_bytes() == b"png-source"
            if outcome == "error":
                raise RuntimeError("engine failure")
            if outcome == "cancel":
                raise asyncio.CancelledError()
            yield SDKMessage(type="result", data={"reason": "completed", "usage": {}})

    def _build_engine(_definition: Any, **kwargs: Any) -> _Engine:
        observed["tool_extra"] = kwargs["tool_extra"]
        return _Engine(kwargs["tool_extra"])

    original_runtime = controller._runtime
    controller._runtime = SimpleNamespace(
        checkpoint_store=original_runtime.checkpoint_store,
        resolve=original_runtime.resolve,
        build_engine=_build_engine,
    )  # type: ignore[assignment]

    response = await controller.run(
        "inspect the uploaded image",
        session_id,
        user_id=user_id,
        attachments=[attachment.storage_path],
    )

    staged = observed["staged"]
    assert observed["exists_during_engine"] is True
    assert observed["context_attachment_path"] == str(staged)
    assert isinstance(observed["prompt"], dict)
    content = observed["prompt"]["content"]
    assert any(
        item.get("type") == "image_url" and item["image_url"]["url"].startswith("data:image/png;base64,")
        for item in content
    )
    lookup = observed["tool_extra"]["attachment_lookup"]["by_name"]
    assert lookup[normalise_attachment_alias("photo.png")] == str(staged)
    assert not staged.exists()
    assert file_service.download_calls == 1
    assert manager.calls == [(session_id, user_id)]
    if outcome == "complete":
        assert response.error is None
    else:
        assert response.error is not None
