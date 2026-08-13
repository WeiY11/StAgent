"""Service-manager wiring for optional object storage."""

from __future__ import annotations

from typing import Any

import pytest

from leagent.config.settings import ObjectStorageSettings, Settings
from leagent.services.service_manager import ServiceManager


@pytest.mark.asyncio
async def test_object_storage_enabled_uses_s3_backend_and_exposes_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = object()
    client = object()
    captured: dict[str, Any] = {}

    def build(settings: ObjectStorageSettings) -> tuple[object, object]:
        captured["settings"] = settings
        return backend, client

    monkeypatch.setattr("leagent.file.storage.s3.build_s3_storage_backend", build)
    settings = Settings(object_storage=ObjectStorageSettings(enabled=True, bucket="kb"))
    manager = ServiceManager(settings)

    await manager._start_file_service()

    assert captured["settings"] is settings.object_storage
    assert manager.file_store is backend
    assert manager.minio is client
    assert manager.file_service is not None
    assert manager.file_service.get_backend("s3") is backend


@pytest.mark.asyncio
async def test_enabled_object_storage_failure_is_not_silently_downgraded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def build(_settings: ObjectStorageSettings) -> tuple[object, object]:
        raise RuntimeError("unreachable object store")

    monkeypatch.setattr("leagent.file.storage.s3.build_s3_storage_backend", build)
    manager = ServiceManager(
        Settings(object_storage=ObjectStorageSettings(enabled=True, bucket="kb"))
    )

    with pytest.raises(RuntimeError, match="unreachable object store"):
        await manager._start_file_service()
