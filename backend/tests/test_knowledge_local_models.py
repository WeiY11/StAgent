"""Focused safety tests for managed local embedding models."""

from __future__ import annotations

import json
from pathlib import Path  # noqa: TC003

import pytest

from leagent.knowledge.local_models import LocalModelRegistry, ModelValidationError


def _safe_model(directory: Path) -> None:
    directory.mkdir()
    (directory / "config.json").write_text(json.dumps({"architectures": ["BertModel"]}), encoding="utf-8")
    (directory / "model.safetensors").write_bytes(b"safe-weights")


def _registry(home: Path) -> LocalModelRegistry:
    return LocalModelRegistry(home, probe_vector=lambda _: (0.25, 0.75))


def test_repo_id_validation_accepts_only_owner_repo() -> None:
    assert LocalModelRegistry.validate_huggingface_repo_id("BAAI/bge-small-zh-v1.5") == "BAAI/bge-small-zh-v1.5"
    for value in ("bge-small", "owner/repo/extra", "../owner/repo", "owner/.repo", "owner/repo.git"):
        with pytest.raises(ModelValidationError):
            LocalModelRegistry.validate_huggingface_repo_id(value)


def test_import_rejects_symlink_and_unsafe_weights(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    unsafe = tmp_path / "unsafe"
    _safe_model(unsafe)
    (unsafe / "pytorch_model.bin").write_bytes(b"unsafe")
    with pytest.raises(ModelValidationError, match="unsafe"):
        registry.import_local(unsafe, display_name="unsafe")

    linked = tmp_path / "linked"
    _safe_model(linked)
    try:
        (linked / "alias.safetensors").symlink_to(linked / "model.safetensors")
    except OSError:
        pytest.skip("symlink creation unavailable")
    with pytest.raises(ModelValidationError, match="symlink"):
        registry.import_local(linked, display_name="linked")


def test_import_cleans_staging_and_publishes_atomically(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _safe_model(source)
    registry = _registry(tmp_path)

    record = registry.import_local(source, display_name="local")

    assert record.status == "ready"
    assert (registry.models_dir / record.model_id / "model.safetensors").is_file()
    assert list(registry.staging_dir.iterdir()) == []
    assert registry.get(record.model_id) == record


def test_fingerprint_is_stable_and_changes_for_encoding_settings(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _safe_model(source)
    registry = _registry(tmp_path)
    first = registry.import_local(source, display_name="same", query_prompt="q: ")
    second = registry.import_local(source, display_name="same", query_prompt="q: ")
    different = registry.import_local(source, display_name="same", query_prompt="question: ")

    assert first.fingerprint == second.fingerprint
    assert first.fingerprint != different.fingerprint


def test_fingerprint_includes_verified_probe_dimension(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _safe_model(source)
    first = LocalModelRegistry(tmp_path, probe_vector=lambda _: (0.25, 0.75)).import_local(source, display_name="same")
    second = LocalModelRegistry(tmp_path, probe_vector=lambda _: (0.25, 0.75, 1.0)).import_local(
        source, display_name="same"
    )

    assert first.dimension == 2
    assert second.dimension == 3
    assert first.fingerprint != second.fingerprint


def test_registry_rejects_symlinked_models_root(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    models_root = tmp_path / "knowledge" / "models"
    models_root.parent.mkdir()
    try:
        models_root.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation unavailable")

    with pytest.raises(ModelValidationError, match="symlink"):
        _registry(tmp_path)
