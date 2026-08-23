# RAG Embedding Runtime Basics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the optional Milvus semantic-retrieval plane capability-aware, explicitly configurable, and safely observable without requiring a model download or a new API key.

**Architecture:** The lexical SQLite/BM25 pipeline remains authoritative and always starts. A small knowledge-layer factory resolves an embedding route from configured provider/model values and only returns an `LLMServiceEmbeddingProvider` when the registered provider advertises embedding support. `ServiceManager` uses that factory before creating `KnowledgeVectorIndex`, clears stale global state on every bootstrap, and exposes a secret-free runtime summary through the existing detailed health endpoint.

**Tech Stack:** Python 3.11, Pydantic settings, existing `LLMService`, existing provider registry, Milvus adapter, pytest.

## Global Constraints

- Do not download BGE or any other local model.
- Do not use or require an OpenAI key.
- Do not call a provider, Milvus, OpenSearch, or reranker while implementing this phase.
- Preserve the current SQLite/BM25 fallback and all unrelated dirty-worktree changes.
- Add contract tests with the code, but defer executing the test suite and live validation until the follow-up verification phase requested by the user.

---

### Task 1: Add explicit knowledge-embedding configuration and a capability-aware resolver

**Files:**
- Modify: `backend/StAgent/config/settings/services.py`
- Modify: `backend/StAgent/memory/embeddings.py`
- Create: `backend/StAgent/knowledge/embedding_runtime.py`
- Create: `backend/tests/test_knowledge_embedding_runtime.py`

**Interfaces:**
- `KnowledgeRetrievalSettings` gains `embedding_enabled`, `embedding_provider`, `embedding_model`, and `embedding_dimension` settings under `StAgent_KNOWLEDGE_`.
- `LLMServiceEmbeddingProvider` accepts an optional `provider: str | None` and passes it through to `LLMService.embed`.
- `resolve_knowledge_embedding_runtime(...) -> KnowledgeEmbeddingRuntime` returns a provider only for a registered provider whose `supports_embeddings` flag is true.

- [x] **Step 1: Write the deferred contract tests**

```python
def test_deepseek_route_is_reported_unavailable_for_embeddings() -> None:
    runtime = resolve_knowledge_embedding_runtime(
        llm_service=_service_with_provider("deepseek", supports_embeddings=False),
        cache=None,
        enabled=True,
        provider="deepseek",
        model="deepseek-v4-flash",
        dimension=1024,
    )
    assert runtime.provider is None
    assert runtime.status.reason == "provider_lacks_embeddings"
```

```python
def test_embedding_capable_provider_is_bound_explicitly() -> None:
    runtime = resolve_knowledge_embedding_runtime(
        llm_service=_service_with_provider("ollama", supports_embeddings=True),
        cache=None,
        enabled=True,
        provider="ollama",
        model="nomic-embed-text",
        dimension=768,
    )
    assert isinstance(runtime.provider, LLMServiceEmbeddingProvider)
    assert runtime.status.provider == "ollama"
    assert runtime.status.model == "nomic-embed-text"
```

- [x] **Step 2: Add settings with conservative defaults**

```python
embedding_enabled: bool = False
embedding_provider: str = ""
embedding_model: str = ""
embedding_dimension: int = Field(default=1024, ge=8, le=16384)
```

- [x] **Step 3: Preserve the selected provider in embedding calls and cache identity**

```python
class LLMServiceEmbeddingProvider:
    def __init__(self, llm_service, *, cache=None, provider=None, model=None, dimension=1024):
        self._provider = provider or None

    async def _embed_uncached(self, texts):
        return await self._llm.embed(texts, provider=self._provider, model=self._model)
```

The cache resource identity includes both provider and model, preventing equal model names from different providers sharing vectors.

- [x] **Step 4: Implement the pure resolver**

```python
@dataclass(frozen=True)
class KnowledgeEmbeddingStatus:
    enabled: bool
    provider: str | None
    model: str | None
    dimension: int
    reason: str

@dataclass(frozen=True)
class KnowledgeEmbeddingRuntime:
    provider: EmbeddingProvider | None
    status: KnowledgeEmbeddingStatus
```

The resolver returns `disabled`, `llm_service_unavailable`, `embedding_route_unconfigured`, `provider_not_registered`, or `provider_lacks_embeddings` without network access. It returns `ready` only after resolving a registered provider with `supports_embeddings=True`.

- [ ] **Step 5: Deferred verification command**

Run later: `cd backend && uv run pytest tests/test_knowledge_embedding_runtime.py -q`

### Task 2: Wire the resolver into the RAG bootstrap and health summary

**Files:**
- Modify: `backend/StAgent/services/service_manager.py`
- Modify: `backend/tests/test_cache_service_manager.py`

**Interfaces:**
- `ServiceManager.knowledge_retrieval_status` returns only booleans, provider/model names, dimensions, and reason codes; it never includes keys, tokens, URLs, or document content.
- `_start_knowledge_retrieval()` clears `set_knowledge_vector_index(None)` before resolving the current runtime and creates `KnowledgeVectorIndex` only when `KnowledgeEmbeddingRuntime.provider` exists.

- [x] **Step 1: Write deferred bootstrap contract tests**

```python
async def test_knowledge_bootstrap_keeps_vector_index_unset_for_deepseek_only(monkeypatch):
    manager = _started_manager_with_deepseek_only_and_milvus_enabled()
    await manager._start_knowledge_retrieval()
    assert get_knowledge_vector_index() is None
    assert manager.knowledge_retrieval_status["vector"]["reason"] == "provider_lacks_embeddings"
```

```python
async def test_knowledge_bootstrap_wires_vector_index_for_embedding_provider(monkeypatch):
    manager = _started_manager_with_embedding_provider_and_milvus_enabled()
    await manager._start_knowledge_retrieval()
    assert get_knowledge_vector_index() is not None
    assert manager.knowledge_retrieval_status["vector"]["reason"] == "ready"
```

- [x] **Step 2: Store and expose the non-secret runtime summary**

```python
@property
def knowledge_retrieval_status(self) -> dict[str, Any]:
    return {
        "query_rewrite_enabled": self.settings.knowledge.query_rewrite_enabled,
        "vector": self._knowledge_embedding_status.to_dict(),
        "opensearch_configured": bool(self.settings.knowledge.opensearch_url),
        "rerank_configured": bool(self.settings.knowledge.rerank_endpoint),
    }
```

- [x] **Step 3: Create the vector index only for a ready runtime**

```python
set_knowledge_vector_index(None)
runtime = resolve_knowledge_embedding_runtime(...)
if cfg.milvus_enabled and runtime.provider is not None:
    set_knowledge_vector_index(KnowledgeVectorIndex(
        embeddings=runtime.provider,
        connection=connection,
        index_version=cfg.index_version,
    ))
```

- [x] **Step 4: Add the knowledge component to `ServiceManager.health_check()`**

```python
results["services"]["knowledge_retrieval"] = {
    "status": "ready" if vector_reason in {"ready", "disabled"} else "degraded",
    "healthy": True,
    **self.knowledge_retrieval_status,
}
```

- [ ] **Step 5: Deferred verification command**

Run later: `cd backend && uv run pytest tests/test_cache_service_manager.py tests/test_knowledge_embedding_runtime.py -q`

### Task 3: Document the runtime contract without enabling a provider

**Files:**
- Modify: `deploy/.env.example`
- Modify: `deploy/README.md`
- Modify: `deploy/helm/StAgent/README.md`
- Modify: `deploy/helm/StAgent/values.yaml`

**Interfaces:**
- `StAgent_KNOWLEDGE_EMBEDDING_ENABLED=false` remains the default.
- The docs name the provider/model/dimension fields and state that DeepSeek-only deployments remain lexical/BM25.

- [x] **Step 1: Add commented non-secret environment examples**

```dotenv
# StAgent_KNOWLEDGE_EMBEDDING_ENABLED=false
# StAgent_KNOWLEDGE_EMBEDDING_PROVIDER=ollama
# StAgent_KNOWLEDGE_EMBEDDING_MODEL=nomic-embed-text
# StAgent_KNOWLEDGE_EMBEDDING_DIMENSION=768
```

- [x] **Step 2: State the safe transition rule**

Document that enabling Milvus without an embedding-capable provider leaves vector retrieval disabled with a visible reason and does not replace lexical retrieval.

- [ ] **Step 3: Deferred verification command**

Run later: `cd backend && uv run ruff check StAgent/knowledge/embedding_runtime.py StAgent/memory/embeddings.py StAgent/services/service_manager.py tests/test_knowledge_embedding_runtime.py && uv run pytest tests/test_cache_service_manager.py tests/test_knowledge_embedding_runtime.py -q`

## Deferred Acceptance Phase

After an embedding provider is authorized and configured, run a real upload/index/search/delete cycle against that provider and Milvus, then run the repository regression suite, the gold query/citation evaluation, OpenSearch/reranker smoke tests, and multi-pod resume/WebSocket validation. No result from the implementation phase may be presented as evidence that those live gates passed.
