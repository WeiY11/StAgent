"""Deterministic tests for the S3-compatible managed-file backend."""

from __future__ import annotations

from io import BytesIO
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

from leagent.config.settings import ObjectStorageSettings, Settings
from leagent.db.service import DatabaseService
from leagent.file.service import FileService
from leagent.file.storage.s3 import S3StorageBackend, build_s3_storage_backend

if TYPE_CHECKING:
    from pathlib import Path


class _NotFoundError(Exception):
    response = {"Error": {"Code": "NoSuchKey"}}


class FakeS3Client:
    """Small in-memory boto3-client substitute; it never opens a socket."""

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], dict[str, Any]] = {}
        self.presign_calls: list[dict[str, Any]] = []

    def put_object(self, **kwargs: Any) -> None:
        self.objects[(kwargs["Bucket"], kwargs["Key"])] = {
            "body": bytes(kwargs["Body"]),
            "content_type": kwargs.get("ContentType", "application/octet-stream"),
            "metadata": kwargs.get("Metadata", {}),
        }

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        bucket = str(kwargs["Bucket"])
        key = str(kwargs["Key"])
        try:
            entry = self.objects[(bucket, key)]
        except KeyError as exc:
            raise _NotFoundError() from exc
        return {
            "Body": BytesIO(entry["body"]),
            "ContentType": entry["content_type"],
        }

    def head_object(self, **kwargs: Any) -> None:
        if (str(kwargs["Bucket"]), str(kwargs["Key"])) not in self.objects:
            raise _NotFoundError()

    def delete_object(self, **kwargs: Any) -> None:
        self.objects.pop((str(kwargs["Bucket"]), str(kwargs["Key"])), None)

    def generate_presigned_url(self, operation: str, **kwargs: Any) -> str:
        self.presign_calls.append({"operation": operation, **kwargs})
        return "https://object.example.test/signed"


@pytest.mark.asyncio
async def test_s3_backend_round_trips_key_without_network() -> None:
    client = FakeS3Client()
    backend = S3StorageBackend(client, bucket="kb", prefix="leagent")

    result = await backend.put(
        b"evidence",
        "docs/a.txt",
        content_type="text/plain",
        metadata={"source": "unit-test"},
    )

    assert result["size"] == len(b"evidence")
    assert result["storage_path"] == "s3://kb/leagent/docs/a.txt"
    assert client.objects[("kb", "leagent/docs/a.txt")]["metadata"] == {
        "source": "unit-test"
    }
    assert await backend.get("docs/a.txt") == (b"evidence", "text/plain")
    assert await backend.exists("docs/a.txt") is True
    assert await backend.presign("docs/a.txt", expires_in=120) == "https://object.example.test/signed"
    assert client.presign_calls[0]["Params"] == {"Bucket": "kb", "Key": "leagent/docs/a.txt"}
    assert await backend.delete("docs/a.txt") is True
    assert await backend.exists("docs/a.txt") is False


def test_s3_backend_recovers_logical_key_from_its_persisted_uri() -> None:
    backend = S3StorageBackend(FakeS3Client(), bucket="kb", prefix="leagent")

    assert backend.key_from_storage_path("s3://kb/leagent/docs/a.txt") == "docs/a.txt"
    assert backend.key_from_storage_path("s3://other/leagent/docs/a.txt") is None
    assert backend.key_from_storage_path("s3://kb/other/docs/a.txt") is None


@pytest.mark.asyncio
async def test_s3_backend_rejects_escape_paths_and_never_mutates_other_keys() -> None:
    backend = S3StorageBackend(FakeS3Client(), bucket="kb", prefix="tenant-a")

    for key in ("../other.txt", "/other.txt", "docs/../../other.txt", ""):
        with pytest.raises(ValueError, match="relative"):
            await backend.put(b"x", key)


def test_s3_factory_accepts_injected_client_without_optional_dependency() -> None:
    client = FakeS3Client()
    settings = ObjectStorageSettings(enabled=True, bucket="kb", prefix="tenant-a")

    backend, returned_client = build_s3_storage_backend(settings, client=client)

    assert returned_client is client
    assert backend.bucket == "kb"
    assert backend.prefix == "tenant-a"


@pytest.mark.asyncio
async def test_file_service_reconstructs_s3_ref_after_restart(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The DB URI must recover the S3 key, rather than becoming a local path."""
    db_path = tmp_path / "s3-files.db"
    monkeypatch.setenv("DB_DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    db = DatabaseService(Settings())
    await db.create_tables()
    try:
        client = FakeS3Client()
        backend = S3StorageBackend(client, bucket="kb", prefix="leagent")
        first = FileService(
            default_backend=backend,
            default_backend_name="s3",
            database=db,
        )
        ref = await first.register(b"durable evidence", filename="evidence.txt")

        restarted = FileService(
            default_backend=backend,
            default_backend_name="s3",
            database=db,
        )
        resolved = await restarted.resolve(ref.id)

        assert resolved is not None
        assert resolved.backend_name == "s3"
        assert resolved.storage_key == ref.storage_key
        assert await restarted.download(resolved) == (b"durable evidence", "text/plain")
    finally:
        await db.dispose()


@pytest.mark.asyncio
async def test_file_api_helper_reads_s3_catalog_record_without_local_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from leagent.api.v1.files import _read_managed_file_bytes

    client = FakeS3Client()
    service = FileService(
        default_backend=S3StorageBackend(client, bucket="kb", prefix="leagent"),
        default_backend_name="s3",
    )
    ref = await service.register(b"remote catalog evidence", filename="evidence.txt")
    db_marker = object()
    manager = SimpleNamespace(file_service=service, db=db_marker)
    monkeypatch.setattr(
        "leagent.services.service_manager.get_service_manager",
        lambda: manager,
    )
    row = SimpleNamespace(
        id=ref.id,
        name=ref.filename,
        storage_path=ref.metadata["storage_path"],
        mime_type=ref.content_type,
        size=ref.size,
        checksum=ref.checksum,
        user_id=None,
        session_id=None,
        library_scope="knowledge",
    )

    assert await _read_managed_file_bytes(row, db_marker) == (
        b"remote catalog evidence",
        "text/plain",
    )


@pytest.mark.asyncio
async def test_file_gc_deletes_exclusive_s3_blob_through_file_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from leagent.library.gc import _remove_orphaned_blob

    client = FakeS3Client()
    backend = S3StorageBackend(client, bucket="kb", prefix="leagent")
    service = FileService(default_backend=backend, default_backend_name="s3")
    ref = await service.register(b"expired evidence", filename="expired.txt")
    db_marker = object()
    manager = SimpleNamespace(file_service=service, db=db_marker)
    monkeypatch.setattr(
        "leagent.services.service_manager.get_service_manager",
        lambda: manager,
    )

    assert await _remove_orphaned_blob(db_marker, ref.id, ref.metadata["storage_path"])
    assert await backend.exists(ref.storage_key) is False
