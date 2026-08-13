"""Settings and static deployment contracts for optional production runtimes."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from leagent.config.settings import Settings


def _clear_runtime_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "LEAGENT_REDIS_ENABLED",
        "LEAGENT_REDIS_URL",
        "LEAGENT_REDIS_PREFIX",
        "LEAGENT_OBJECT_STORAGE_ENABLED",
        "LEAGENT_OBJECT_STORAGE_BUCKET",
        "LEAGENT_OBJECT_STORAGE_ENDPOINT_URL",
        "LEAGENT_OBJECT_STORAGE_PREFIX",
        "LEAGENT_KNOWLEDGE_INDEX_VERSION",
    ):
        monkeypatch.delenv(key, raising=False)


def test_external_runtime_settings_default_to_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_runtime_env(monkeypatch)

    settings = Settings()

    assert settings.redis.enabled is False
    assert settings.object_storage.enabled is False


def test_external_runtime_settings_load_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_runtime_env(monkeypatch)
    monkeypatch.setenv("LEAGENT_REDIS_ENABLED", "true")
    monkeypatch.setenv("LEAGENT_REDIS_URL", "redis://cache:6379/2")
    monkeypatch.setenv("LEAGENT_REDIS_PREFIX", "production")
    monkeypatch.setenv("LEAGENT_OBJECT_STORAGE_ENABLED", "true")
    monkeypatch.setenv("LEAGENT_OBJECT_STORAGE_BUCKET", "leagent-files")
    monkeypatch.setenv("LEAGENT_OBJECT_STORAGE_ENDPOINT_URL", "https://minio.example.test")
    monkeypatch.setenv("LEAGENT_OBJECT_STORAGE_PREFIX", "tenant-a/uploads/")

    settings = Settings()

    assert settings.redis.url == "redis://cache:6379/2"
    assert settings.redis.prefix == "production"
    assert settings.object_storage.bucket == "leagent-files"
    assert settings.object_storage.prefix == "tenant-a/uploads"


def test_enabled_external_runtime_rejects_incomplete_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_runtime_env(monkeypatch)
    monkeypatch.setenv("LEAGENT_REDIS_ENABLED", "true")
    with pytest.raises(ValidationError, match="Redis URL"):
        Settings()

    _clear_runtime_env(monkeypatch)
    monkeypatch.setenv("LEAGENT_OBJECT_STORAGE_ENABLED", "true")
    with pytest.raises(ValidationError, match="bucket"):
        Settings()


def test_knowledge_index_version_is_bounded_and_storage_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_runtime_env(monkeypatch)
    monkeypatch.setenv("LEAGENT_KNOWLEDGE_INDEX_VERSION", "embedding-v2.1")

    assert Settings().knowledge.index_version == "embedding-v2.1"

    monkeypatch.setenv("LEAGENT_KNOWLEDGE_INDEX_VERSION", "embedding version 2")
    with pytest.raises(ValidationError, match="index_version"):
        Settings()

    monkeypatch.setenv("LEAGENT_KNOWLEDGE_INDEX_VERSION", "x" * 65)
    with pytest.raises(ValidationError, match="index_version"):
        Settings()


def test_helm_values_keep_object_storage_credentials_out_of_defaults() -> None:
    root = Path(__file__).resolve().parents[2]
    values = (root / "deploy" / "helm" / "leagent" / "values.yaml").read_text(encoding="utf-8")
    deployment = (root / "deploy" / "helm" / "leagent" / "templates" / "deployment.yaml").read_text(encoding="utf-8")

    assert "LEAGENT_OBJECT_STORAGE_ACCESS_KEY_ID" not in values
    assert "LEAGENT_OBJECT_STORAGE_SECRET_ACCESS_KEY" not in values
    assert 'LEAGENT_REDIS_ENABLED: "false"' in values
    assert 'LEAGENT_OBJECT_STORAGE_ENABLED: "false"' in values
    assert "LEAGENT_OBJECT_STORAGE_BUCKET" in values
    assert "name: LEAGENT_REDIS_URL" in deployment
    assert "name: LEAGENT_OBJECT_STORAGE_ACCESS_KEY_ID" in deployment
    assert "secretKeyRef:" in deployment
