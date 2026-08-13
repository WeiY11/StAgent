from __future__ import annotations

from typing import Any

import pytest

from leagent.config.settings import Settings
from leagent.knowledge.query import LLMQueryRewriter
from leagent.knowledge.vector_index import (
    get_knowledge_vector_index,
    set_knowledge_vector_index,
)
from leagent.services.base import ServiceState
from leagent.services.service_manager import ServiceManager
from leagent.workflow.engine import CacheServiceWorkflowProvider


@pytest.mark.asyncio
async def test_cache_service_lifecycle_is_owned_by_manager() -> None:
    manager = ServiceManager(Settings())
    await manager._start_cache()
    assert manager.cache is not None
    assert manager.cache.state is ServiceState.READY
    await manager.cache.stop()


@pytest.mark.asyncio
async def test_chat_receives_manager_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = ServiceManager(Settings())
    manager._db = object()  # type: ignore[assignment]
    await manager._start_cache()
    captured = {}

    async def fake_init(settings, database, cache):  # noqa: ANN001
        captured["cache"] = cache
        return object()

    monkeypatch.setattr("leagent.services.chat.service.init_chat_service", fake_init)
    await manager._start_chat()
    assert captured["cache"] is manager.cache
    await manager.cache.stop()


@pytest.mark.asyncio
async def test_session_manager_receives_manager_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = ServiceManager(Settings())
    manager._db = object()  # type: ignore[assignment]
    await manager._start_cache()
    captured = {}

    class FakeSessionManager:
        def __init__(self, settings, *, database, file_service=None, cache=None):  # noqa: ANN001
            captured["cache"] = cache

    monkeypatch.setattr(
        "leagent.services.session.manager.SessionManager",
        FakeSessionManager,
    )
    await manager._start_session_manager()
    assert captured["cache"] is manager.cache
    await manager.cache.stop()


@pytest.mark.asyncio
async def test_workflow_executor_receives_cache_service_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = ServiceManager(Settings())
    manager._db = object()  # type: ignore[assignment]
    manager._runtime_context = type("RuntimeContextStub", (), {"executor": object()})()  # type: ignore[assignment]
    await manager._start_cache()
    captured: dict[str, Any] = {}

    async def fake_bootstrap(*args, **kwargs):  # noqa: ANN002, ANN003
        return None

    class FakeEventBus:
        async def publish_event(self, prompt_id, event):  # noqa: ANN001
            return None

    class FakeWorkflowExecutor:
        def __init__(self, **kwargs):  # noqa: ANN003
            captured.update(kwargs)

        def register_progress_handler(self, handler):  # noqa: ANN001
            captured["progress_handler"] = handler

    class FakeWorkflowService:
        def __init__(self, **kwargs):  # noqa: ANN003
            captured["workflow_service_kwargs"] = kwargs

    monkeypatch.setattr("leagent.workflow.nodes.bootstrap", fake_bootstrap)
    monkeypatch.setattr("leagent.workflow.server.event_bus.InMemoryEventBus", FakeEventBus)
    monkeypatch.setattr("leagent.workflow.engine.WorkflowExecutor", FakeWorkflowExecutor)
    monkeypatch.setattr("leagent.workflow.registry.FlowWorkflowRegistry", lambda db: object())
    monkeypatch.setattr("leagent.workflow.state_store.build_workflow_state_store", lambda db: object())
    monkeypatch.setattr("leagent.workflow.prompt_map.build_prompt_map", lambda value: object())
    monkeypatch.setattr("leagent.workflow.services.WorkflowService", FakeWorkflowService)
    monkeypatch.setattr("leagent.tools.registry.get_registry", lambda: object())

    await manager._start_workflow_service()

    provider = captured["cache_provider"]
    assert isinstance(provider, CacheServiceWorkflowProvider)
    assert provider._cache is manager.cache
    assert manager.workflow_service is not None
    await manager.cache.stop()


@pytest.mark.asyncio
async def test_knowledge_pipeline_uses_llm_rewriter_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An enabled rewrite policy receives the configured LLM at RAG bootstrap."""
    settings = Settings()
    settings.knowledge.query_rewrite_enabled = True
    manager = ServiceManager(settings)
    manager._db = object()  # type: ignore[assignment]
    manager._llm_service = object()  # type: ignore[assignment]
    captured: dict[str, Any] = {}

    class _Pipeline:
        def __init__(self, _db: object, **kwargs: object) -> None:
            captured.update(kwargs)

    monkeypatch.setattr("leagent.library.retrieval.KnowledgeRetrievalPipeline", _Pipeline)
    monkeypatch.setattr(
        "leagent.library.retrieval.set_knowledge_retrieval_pipeline",
        lambda pipeline: captured.setdefault("pipeline", pipeline),
    )

    await manager._start_knowledge_retrieval()

    assert isinstance(captured["query_rewriter"], LLMQueryRewriter)


@pytest.mark.asyncio
async def test_knowledge_bootstrap_keeps_vector_index_unset_for_deepseek_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A chat-only DeepSeek route must not create a misleading vector index."""
    settings = Settings()
    settings.knowledge.milvus_enabled = True
    settings.knowledge.embedding_enabled = True
    settings.knowledge.embedding_provider = "deepseek"
    settings.knowledge.embedding_model = "deepseek-v4-flash"
    manager = ServiceManager(settings)
    manager._db = object()  # type: ignore[assignment]

    class _Registry:
        def has_provider(self, name: str) -> bool:
            return name == "deepseek"

        def get_provider(self, name: str) -> object:
            assert name == "deepseek"
            return type("DeepSeek", (), {"supports_embeddings": False})()

    manager._llm_service = type("LLM", (), {"registry": _Registry()})()  # type: ignore[assignment]

    class _Pipeline:
        def __init__(self, _db: object, **_kwargs: object) -> None:
            return None

    monkeypatch.setattr("leagent.library.retrieval.KnowledgeRetrievalPipeline", _Pipeline)
    monkeypatch.setattr(
        "leagent.library.retrieval.set_knowledge_retrieval_pipeline",
        lambda _pipeline: None,
    )

    await manager._start_knowledge_retrieval()

    assert get_knowledge_vector_index() is None
    assert manager.knowledge_retrieval_status["vector"]["reason"] == "provider_lacks_embeddings"


@pytest.mark.asyncio
async def test_knowledge_bootstrap_wires_vector_index_for_embedding_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A declared embedding-capable provider enables the optional vector plane."""
    settings = Settings()
    settings.knowledge.milvus_enabled = True
    settings.knowledge.embedding_enabled = True
    settings.knowledge.embedding_provider = "ollama"
    settings.knowledge.embedding_model = "nomic-embed-text"
    settings.knowledge.embedding_dimension = 768
    manager = ServiceManager(settings)
    manager._db = object()  # type: ignore[assignment]

    class _Registry:
        def has_provider(self, name: str) -> bool:
            return name == "ollama"

        def get_provider(self, name: str) -> object:
            assert name == "ollama"
            return type("Ollama", (), {"supports_embeddings": True})()

    manager._llm_service = type("LLM", (), {"registry": _Registry()})()  # type: ignore[assignment]

    class _Pipeline:
        def __init__(self, _db: object, **_kwargs: object) -> None:
            return None

    class _Index:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs

    monkeypatch.setattr("leagent.library.retrieval.KnowledgeRetrievalPipeline", _Pipeline)
    monkeypatch.setattr(
        "leagent.library.retrieval.set_knowledge_retrieval_pipeline",
        lambda _pipeline: None,
    )
    monkeypatch.setattr("leagent.knowledge.vector_index.KnowledgeVectorIndex", _Index)

    try:
        await manager._start_knowledge_retrieval()
        assert isinstance(get_knowledge_vector_index(), _Index)
        assert manager.knowledge_retrieval_status["vector"] == {
            "enabled": True,
            "provider": "ollama",
            "model": "nomic-embed-text",
            "dimension": 768,
            "reason": "ready",
        }
    finally:
        set_knowledge_vector_index(None)
