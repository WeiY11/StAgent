"""Focused contracts for local semantic runtime wiring and dual writes."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest

import leagent.config.config as runtime_config
from leagent.config.settings import Settings
from leagent.knowledge.semantic_runtime import (
    CandidateDualWriteIndex,
    KnowledgeSemanticRebuilder,
    SemanticRuntimeState,
    redact_semantic_error,
)
from leagent.services.service_manager import ServiceManager

if TYPE_CHECKING:
    from pathlib import Path


class _Backend:
    def __init__(self) -> None:
        self.available = True
        self.index_version = "semantic-model-a"
        self.upserts: list[str | None] = []
        self.deletes: list[str | None] = []
        self.discards: list[str] = []

    async def upsert_chunks(self, *args: object, index_version: str | None = None, **kwargs: object) -> int:
        self.upserts.append(index_version)
        return 1

    async def delete_for_file(self, *args: object, index_version: str | None = None, **kwargs: object) -> int:
        self.deletes.append(index_version)
        return 1

    async def embed_query(self, query: str) -> list[float]:
        return [1.0]

    async def search_vector(self, query_vector: list[float], **kwargs: object) -> list[tuple[str, float]]:
        return [("chunk", 1.0)]

    async def begin_version(self, index_version: str) -> bool:
        return True

    async def activate_version(self, index_version: str) -> bool:
        self.index_version = index_version
        return True

    async def discard_version(self, index_version: str) -> bool:
        self.discards.append(index_version)
        return True


class _LifecycleBackend(_Backend):
    """In-memory projection backend that records the atomic swap boundary."""

    def __init__(self) -> None:
        super().__init__()
        self.started: list[str] = []
        self.activated: list[str] = []

    async def begin_version(self, index_version: str) -> bool:
        self.started.append(index_version)
        return True

    async def activate_version(self, index_version: str) -> bool:
        self.activated.append(index_version)
        self.index_version = index_version
        return True


def test_environment_semantic_settings_take_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEAGENT_KNOWLEDGE_SEMANTIC_ENABLED", "true")
    monkeypatch.setenv("LEAGENT_KNOWLEDGE_SEMANTIC_LOCAL_MODEL_ID", "local-explicit")

    semantic = Settings().knowledge.semantic

    assert semantic.enabled is True
    assert semantic.local_model_id == "local-explicit"
    assert semantic.environment_managed is True


def test_nested_environment_semantic_settings_are_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LEAGENT_KNOWLEDGE_SEMANTIC_ENABLED", raising=False)
    monkeypatch.delenv("LEAGENT_KNOWLEDGE_SEMANTIC_LOCAL_MODEL_ID", raising=False)
    monkeypatch.setenv("LEAGENT_KNOWLEDGE__SEMANTIC__ENABLED", "true")
    monkeypatch.setenv("LEAGENT_KNOWLEDGE__SEMANTIC__LOCAL_MODEL_ID", "nested-model")

    semantic = Settings().knowledge.semantic

    assert semantic.enabled is True
    assert semantic.local_model_id == "nested-model"
    assert semantic.environment_managed is True


def test_yaml_semantic_preferences_are_used_when_environment_is_not_managed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "knowledge:\n  semantic:\n    enabled: true\n    local_model_id: local-yaml\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(runtime_config, "CONFIG_PATH", config_path)
    manager = ServiceManager(Settings())

    effective = manager._effective_knowledge_semantic_config()

    assert effective.enabled is True
    assert effective.local_model_id == "local-yaml"
    assert effective.environment_managed is False


def test_semantic_environment_overrides_only_the_fields_it_manages(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "knowledge:\n  semantic:\n    enabled: false\n    local_model_id: local-yaml\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(runtime_config, "CONFIG_PATH", config_path)
    monkeypatch.setenv("LEAGENT_KNOWLEDGE_SEMANTIC_ENABLED", "true")
    manager = ServiceManager(Settings())

    effective = manager._effective_knowledge_semantic_config()

    assert effective.enabled is True
    assert effective.local_model_id == "local-yaml"
    assert effective.environment_managed is True


@pytest.mark.asyncio
async def test_candidate_dual_writes_active_and_candidate() -> None:
    backend = _Backend()
    index = CandidateDualWriteIndex(
        backend,  # type: ignore[arg-type]
        active_version="semantic-model-a",
        candidate_version="semantic-model-b",
        serve_active=True,
    )
    file_id = uuid4()

    assert index.requires_allowed_file_ids is True
    assert await index.upsert_chunks(file_id, None, []) == 1
    assert await index.delete_for_file(file_id) == 1
    assert backend.upserts == ["semantic-model-b", "semantic-model-a"]
    assert backend.deletes == ["semantic-model-b", "semantic-model-a"]


@pytest.mark.asyncio
async def test_model_switch_candidate_does_not_write_new_vectors_to_old_active() -> None:
    backend = _Backend()
    index = CandidateDualWriteIndex(
        backend,  # type: ignore[arg-type]
        active_version="semantic-old-model",
        candidate_version="semantic-new-model",
        serve_active=False,
    )

    assert await index.upsert_chunks(uuid4(), None, []) == 1
    assert await index.search_vector([1.0], limit=1, allowed_file_ids=[uuid4()]) == []
    assert backend.upserts == ["semantic-new-model"]


@pytest.mark.asyncio
async def test_multiworker_local_mode_degrades_to_lexical_only() -> None:
    manager = ServiceManager(SimpleNamespace(workers=2))  # type: ignore[arg-type]
    await manager._start_local_semantic_retrieval(
        SimpleNamespace(enabled=True, local_model_id="local-explicit", environment_managed=False)
    )

    assert manager.knowledge_semantic_status["status"] == "lexical_only"
    assert manager.knowledge_semantic_status["error"] == "local_backend_requires_single_worker_use_milvus"


@pytest.mark.asyncio
async def test_model_switch_keeps_old_active_serving_and_starts_candidate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    old = SimpleNamespace(
        model_id="local-old-model",
        status="ready",
        dimension=3,
        fingerprint="d" * 64,
        query_prompt=None,
        document_prompt=None,
        normalize=True,
    )
    new = SimpleNamespace(
        model_id="local-new-model",
        status="ready",
        dimension=3,
        fingerprint="n" * 64,
        query_prompt=None,
        document_prompt=None,
        normalize=True,
    )
    models_dir = tmp_path / "knowledge" / "models"
    (models_dir / old.model_id).mkdir(parents=True)
    (models_dir / new.model_id).mkdir()

    class _Registry:
        def __init__(self, home: Path) -> None:
            self.models_dir = models_dir

        def get(self, model_id: str) -> object | None:
            return new if model_id == new.model_id else None

        def list(self) -> list[object]:
            return [new, old]

    class _Index(_Backend):
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__()
            self.index_version = "semantic-old-model-dddddddddddd"

        async def version_status(self) -> dict[str, str]:
            return {}

    class _Provider:
        def __init__(self, *args: object, **kwargs: object) -> None:
            return None

    monkeypatch.setenv("LEAGENT_HOME", str(tmp_path))
    monkeypatch.setattr("leagent.knowledge.local_models.LocalModelRegistry", _Registry)
    monkeypatch.setattr("leagent.knowledge.sqlite_vec_index.SqliteVecKnowledgeIndex", _Index)
    monkeypatch.setattr("leagent.memory.embeddings.LocalSentenceTransformerEmbeddingProvider", _Provider)
    manager = ServiceManager(SimpleNamespace(workers=1))  # type: ignore[arg-type]
    await manager._start_local_semantic_retrieval(
        SimpleNamespace(
            local_model_id=new.model_id,
            vector_backend="sqlite-vec",
            auto_reindex=True,
            environment_managed=False,
        )
    )

    assert manager.knowledge_semantic_status["status"] == "building"
    assert manager._knowledge_semantic_auto_rebuild is True
    assert manager.knowledge_semantic_rebuilder._serve_active is True


@pytest.mark.asyncio
async def test_failed_model_switch_restores_old_active_backend() -> None:
    candidate = _Backend()
    active = _Backend()
    events: list[object] = []
    rebuilder = KnowledgeSemanticRebuilder(
        SimpleNamespace(),  # type: ignore[arg-type]
        candidate,  # type: ignore[arg-type]
        SemanticRuntimeState(model_id="local-new"),
        on_index_changed=events.append,
        active_backend=active,  # type: ignore[arg-type]
        serve_active=True,
    )

    async def _fail_count() -> tuple[int, int]:
        raise RuntimeError("candidate source failed")

    rebuilder._count_corpus = _fail_count  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="candidate source failed"):
        await rebuilder.rebuild()

    assert events[-1] is active


@pytest.mark.asyncio
async def test_rebuild_activates_a_complete_candidate_and_replaces_the_serving_projection() -> None:
    """A fully written, unchanged corpus atomically promotes its candidate."""
    backend = _LifecycleBackend()
    events: list[object] = []
    rebuilder = KnowledgeSemanticRebuilder(
        SimpleNamespace(),  # type: ignore[arg-type]
        backend,  # type: ignore[arg-type]
        SemanticRuntimeState(model_id="local-model"),
        on_index_changed=events.append,
    )
    row = SimpleNamespace(
        id=uuid4(),
        file_id=uuid4(),
        user_id=None,
        seq=0,
        start_offset=0,
        end_offset=5,
        text="hello",
        text_hash="content-hash",
    )

    async def _count() -> tuple[int, int]:
        return 1, 1

    async def _generation() -> str:
        return "stable-corpus"

    async def _batch(offset: int) -> list[object]:
        return [row] if offset == 0 else []

    rebuilder._count_corpus = _count  # type: ignore[method-assign]
    rebuilder._corpus_generation = _generation  # type: ignore[method-assign]
    rebuilder._load_batch = _batch  # type: ignore[method-assign]

    result = await rebuilder.rebuild(candidate_version="semantic-candidate")

    assert result == {
        "active_version": "semantic-candidate",
        "chunk_count": 1,
        "document_count": 1,
    }
    assert backend.started == ["semantic-candidate"]
    assert backend.upserts == ["semantic-candidate"]
    assert backend.activated == ["semantic-candidate"]
    assert rebuilder.state.mode == "ready"
    assert rebuilder.state.active_version == "semantic-candidate"
    assert rebuilder.state.candidate_version is None
    assert isinstance(events[0], CandidateDualWriteIndex)
    assert events[0].index_version == "semantic-model-a"
    assert events[-1] is backend


@pytest.mark.asyncio
async def test_rebuild_never_activates_a_candidate_when_corpus_generation_changes() -> None:
    """A stale candidate is discarded and the previous semantic reader is restored."""
    candidate = _LifecycleBackend()
    candidate.index_version = "semantic-old"
    active = _Backend()
    active.index_version = "semantic-old"
    events: list[object] = []
    state = SemanticRuntimeState(model_id="local-new-model")
    rebuilder = KnowledgeSemanticRebuilder(
        SimpleNamespace(),  # type: ignore[arg-type]
        candidate,  # type: ignore[arg-type]
        state,
        on_index_changed=events.append,
        active_backend=active,  # type: ignore[arg-type]
        serve_active=True,
    )
    row = SimpleNamespace(
        id=uuid4(),
        file_id=uuid4(),
        user_id=None,
        seq=0,
        start_offset=0,
        end_offset=5,
        text="hello",
        text_hash="content-hash",
    )
    generations = iter(("before-rebuild", "after-write"))

    async def _count() -> tuple[int, int]:
        return 1, 1

    async def _generation() -> str:
        return next(generations)

    async def _batch(offset: int) -> list[object]:
        return [row] if offset == 0 else []

    rebuilder._count_corpus = _count  # type: ignore[method-assign]
    rebuilder._corpus_generation = _generation  # type: ignore[method-assign]
    rebuilder._load_batch = _batch  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="semantic_corpus_changed_during_rebuild"):
        await rebuilder.rebuild(candidate_version="semantic-candidate")

    assert candidate.upserts == ["semantic-candidate"]
    assert candidate.activated == []
    assert candidate.discards == ["semantic-candidate", "semantic-candidate"]
    assert state.mode == "degraded"
    assert state.active_version == "semantic-old"
    assert state.candidate_version is None
    assert isinstance(events[0], CandidateDualWriteIndex)
    assert events[0].index_version == "semantic-old"
    assert events[-1] is active


@pytest.mark.asyncio
async def test_retry_discards_persisted_candidate_before_rebuild() -> None:
    backend = _Backend()
    rebuilder = KnowledgeSemanticRebuilder(
        SimpleNamespace(),  # type: ignore[arg-type]
        backend,  # type: ignore[arg-type]
        SemanticRuntimeState(model_id="local-model"),
        on_index_changed=lambda _: None,
    )

    async def _empty_count() -> tuple[int, int]:
        return 0, 0

    async def _empty_batch(offset: int) -> list[object]:
        return []

    rebuilder._count_corpus = _empty_count  # type: ignore[method-assign]
    rebuilder._load_batch = _empty_batch  # type: ignore[method-assign]

    await rebuilder.rebuild(candidate_version="semantic-stale-candidate")

    assert backend.discards == ["semantic-stale-candidate"]


def test_semantic_status_redacts_paths_and_state_does_not_expose_them() -> None:
    state = SemanticRuntimeState(mode="degraded", error="C:/private/model/token.txt failed")

    assert redact_semantic_error(state.error) == "semantic_backend_unavailable"
    assert state.to_dict()["error"] == "semantic_backend_unavailable"


def test_semantic_status_exposes_capacity_warning_without_truncating() -> None:
    state = SemanticRuntimeState(chunk_count=100_000, capacity_warning=True)

    payload = state.to_dict()

    assert payload["chunk_count"] == 100_000
    assert payload["capacity_warning"] is True


def test_sidecar_quarantine_preserves_payload_and_journal_files(tmp_path: Path) -> None:
    sidecar = tmp_path / "vectors.sqlite3"
    sidecar.write_bytes(b"SQLite format 3\x00" + b"damaged-payload")
    (tmp_path / "vectors.sqlite3-wal").write_bytes(b"wal")
    (tmp_path / "vectors.sqlite3-shm").write_bytes(b"shm")

    assert ServiceManager._quarantine_vector_sidecar(sidecar) is True
    assert not sidecar.exists()
    quarantined = list(tmp_path.glob("vectors.sqlite3.corrupt-*"))
    assert len(quarantined) == 3
    assert any(path.read_bytes().endswith(b"damaged-payload") for path in quarantined)
    assert any(path.read_bytes() == b"wal" for path in quarantined)
    assert any(path.read_bytes() == b"shm" for path in quarantined)
