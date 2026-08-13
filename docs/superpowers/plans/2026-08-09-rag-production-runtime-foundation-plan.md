# RAG Production Runtime Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve the current single-node default while making Redis, S3-compatible object storage, and answer-level grounding explicit, testable production options.

**Architecture:** Keep SQLite, local files, and in-memory cache as the zero-dependency default. Add optional settings and adapters that are constructed only when their feature flag and complete configuration are present; failures must either fail fast in production mode or leave the default mode intact. Grounded-answer validation stays independent of retrieval so the agent layer can reject missing or invalid citation references without changing lexical retrieval.

**Tech Stack:** Python 3.11, FastAPI, Pydantic settings, redis-py asyncio client, boto3 S3-compatible client, pytest, Helm.

## Execution status (2026-08-09)

- Completed locally: optional Redis/S3 runtime wiring, multi-worker topology gate,
  object-storage restart recovery and garbage collection, tool-driven and
  auto-retrieval answer grounding, visible source legends, and fail-closed
  handling for explicit knowledge-base requests with no evidence. Fresh-worker
  Milvus search now initializes an existing collection before querying, and
  remote lexical version filters now work with dynamic OpenSearch mappings.
  Reindexing deletes stale vector scopes before upsert, isolates non-default
  Milvus collections, carries the configured index version through durable
  jobs/state, and exposes an ACL-scoped, bounded stale-projection rebuild at
  `POST /api/v1/knowledge/reindex-stale`.
- Local verification: focused backend RAG/runtime suite (235 tests), frontend
  knowledge tests (11 tests), frontend production build, YAML parsing, and
  diff whitespace checks. Additional local runtime evidence: real Redis L2
  cross-instance cache/generation/invalidation; real MinIO upload, restart
  recovery, presigned download, and cleanup; an Elasticsearch-compatible
  OpenSearch adapter write/search/delete cycle; a local HTTP reranker response
  contract; production-extra PyMilvus import; Compose rendering; and Helm
  rendering through an ephemeral container.
- Still required before a production claim: an authenticated smoke test against
  the selected OpenSearch distribution, Milvus/vector service and embedding
  provider, and reranker model; a gold query/citation dataset that proves the
  configured quality targets; and a multi-pod pause/resume/WebSocket test.
  Milvus Lite cannot supply that evidence on this Windows host because its
  official supported environments are Linux and macOS.

## Global Constraints

- Default startup remains SQLite + one worker + local file storage + in-memory cache.
- Optional dependencies must be declared in a dedicated `production` extra and imported lazily.
- No object key may escape the configured bucket/prefix; no credential value may be logged.
- Every external backend must have a deterministic fake-client unit test; no external endpoint is treated as verified by unit tests.
- Existing dirty-worktree files outside this scope remain untouched.

---

### Task 1: Define production-runtime settings and dependency boundary

**Files:**
- Modify: `backend/leagent/config/settings/services.py`
- Modify: `backend/leagent/config/settings/__init__.py`
- Modify: `backend/pyproject.toml`
- Test: `backend/tests/test_production_runtime_settings.py`

**Interfaces:**
- Produces `RedisSettings` and `ObjectStorageSettings` settings groups.
- Produces optional `production = ["redis>=5", "boto3>=1"]` dependency extra.

- [ ] **Step 1: Write failing settings tests**

```python
def test_external_runtime_settings_default_to_disabled(monkeypatch):
    monkeypatch.delenv("LEAGENT_REDIS_URL", raising=False)
    assert Settings().redis.enabled is False
    assert Settings().object_storage.enabled is False
```

- [ ] **Step 2: Run the test to verify the setting groups are absent**

Run: `uv run pytest tests/test_production_runtime_settings.py -q`

Expected: FAIL because `Settings` has no `redis` or `object_storage` group.

- [ ] **Step 3: Add the settings groups and optional dependency extra**

```python
class RedisSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LEAGENT_REDIS_")
    enabled: bool = False
    url: str = ""

class ObjectStorageSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LEAGENT_OBJECT_STORAGE_")
    enabled: bool = False
    endpoint_url: str = ""
    bucket: str = ""
```

- [ ] **Step 4: Run settings tests and Ruff**

Run: `uv run pytest tests/test_production_runtime_settings.py -q && uv run ruff check leagent/config/settings/services.py leagent/config/settings/__init__.py tests/test_production_runtime_settings.py`

Expected: PASS.

### Task 2: Add S3-compatible storage backend and FileService wiring

**Files:**
- Create: `backend/leagent/file/storage/s3.py`
- Modify: `backend/leagent/file/storage/__init__.py`
- Modify: `backend/leagent/services/service_manager.py`
- Test: `backend/tests/test_file/test_s3_storage.py`

**Interfaces:**
- Produces `S3StorageBackend(client, bucket, prefix="")` implementing `StorageBackend`.
- `ServiceManager.file_store` and `ServiceManager.minio` expose the configured backend/client.

- [ ] **Step 1: Write fake-client tests**

```python
async def test_s3_backend_round_trips_key_without_network():
    backend = S3StorageBackend(FakeS3Client(), bucket="kb", prefix="leagent")
    await backend.put(b"evidence", "docs/a.txt")
    assert await backend.get("docs/a.txt") == (b"evidence", "text/plain")
```

- [ ] **Step 2: Run the test to verify the backend is absent**

Run: `uv run pytest tests/test_file/test_s3_storage.py -q`

Expected: FAIL because `S3StorageBackend` is absent.

- [ ] **Step 3: Implement safe key normalization, put/get/delete/exists/presign**

```python
class S3StorageBackend:
    async def put(self, data, key, *, content_type="application/octet-stream", metadata=None): ...
    async def get(self, key): ...
    async def delete(self, key): ...
    async def exists(self, key): ...
    async def presign(self, key, *, expires_in=3600): ...
```

- [ ] **Step 4: Construct it only when production object storage is enabled and configured**

```python
if self.settings.object_storage.enabled:
    backend, client = build_s3_storage_backend(self.settings.object_storage)
else:
    backend = LocalStorageBackend(upload_dir)
```

- [ ] **Step 5: Run storage tests and scoped service-manager tests**

Run: `uv run pytest tests/test_file/test_s3_storage.py tests/test_services -q`

Expected: PASS with no endpoint access.

### Task 3: Add Redis cache/run-store runtime wiring

**Files:**
- Create: `backend/leagent/services/cache/redis_backend.py`
- Modify: `backend/leagent/services/cache/service.py`
- Modify: `backend/leagent/services/service_manager.py`
- Modify: `backend/leagent/runtime/deployment_topology.py`
- Test: `backend/tests/test_services/test_redis_runtime.py`

**Interfaces:**
- Produces `RedisCacheBackend(client, prefix)` and `build_redis_client(settings)`.
- `CacheService.using_redis` is true only after a successful ping.
- `set_run_store(build_redis_run_store(sync_client))` is installed only in Redis-enabled multi-worker mode.

- [ ] **Step 1: Write fake Redis tests**

```python
async def test_redis_cache_is_l2_with_memory_fallback():
    service = CacheService(settings, redis_client=FakeRedis())
    await service.set("q", {"answer": "grounded"}, namespace="kb")
    assert await service.get("q", namespace="kb") == {"answer": "grounded"}
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_services/test_redis_runtime.py -q`

Expected: FAIL because `CacheService` has no Redis backend parameter.

- [ ] **Step 3: Implement JSON serialization, TTL propagation, graceful L1 fallback, and startup ping**

```python
class RedisCacheBackend:
    async def get(self, key): ...
    async def set(self, key, value, ttl): ...
    async def delete(self, key): ...
```

- [ ] **Step 4: Enforce topology policy**

```python
if workers > 1 and not redis_enabled:
    raise DeploymentTopologyError("multi-worker mode requires LEAGENT_REDIS_ENABLED=true")
```

- [ ] **Step 5: Run Redis runtime tests and topology tests**

Run: `uv run pytest tests/test_services/test_redis_runtime.py tests/test_deployment_topology.py -q`

Expected: PASS with fake clients only.

### Task 4: Add answer-level citation validation and evaluation contract

**Files:**
- Modify: `backend/leagent/knowledge/evidence.py`
- Modify: `backend/leagent/knowledge/evaluation.py`
- Modify: `backend/leagent/context/sources/knowledge_context.py`
- Test: `backend/tests/test_knowledge_answer_grounding.py`

**Interfaces:**
- Produces `validate_answer_citations(answer: str, evidence: EvidenceBundle, strict: bool) -> AnswerGroundingResult`.
- A strict context attachment requires at least one citation token such as `[K1]` when evidence is supplied.

- [ ] **Step 1: Write failing grounding tests**

```python
def test_strict_grounding_rejects_answer_without_valid_citation():
    result = validate_answer_citations("unsupported answer", evidence, strict=True)
    assert result.accepted is False
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_knowledge_answer_grounding.py -q`

Expected: FAIL because the validator is absent.

- [ ] **Step 3: Implement citation-token parsing and bounded validation**

```python
valid_ids = {citation.citation_id for citation in evidence.citations}
referenced = tuple(dict.fromkeys(re.findall(r"\\[K\\d+\\]", answer)))
```

- [ ] **Step 4: Add strict system-facing instruction to knowledge context**

```text
Use only supported claims and include at least one valid [K#] reference for each answer grounded in this attachment.
```

- [ ] **Step 5: Run answer-grounding and retrieval-quality tests**

Run: `uv run pytest tests/test_knowledge_answer_grounding.py tests/test_knowledge_quality.py -q`

Expected: PASS.

### Task 5: Extend production configuration and verify static deployment contracts

**Files:**
- Modify: `deploy/.env.example`
- Modify: `deploy/helm/leagent/values.yaml`
- Modify: `deploy/helm/leagent/templates/deployment.yaml`
- Modify: `deploy/helm/leagent/README.md`
- Test: `backend/tests/test_production_runtime_settings.py`

**Interfaces:**
- Produces documented environment names for Redis and object storage.
- Helm passes secrets by `secretKeyRef`; no access keys appear in values defaults.

- [ ] **Step 1: Write static configuration tests**

```python
def test_helm_values_never_ship_object_storage_credentials():
    assert "accessKey: \"\"" not in VALUES_TEXT
```

- [ ] **Step 2: Add non-secret endpoint/bucket configuration and Secret references**

```yaml
LEAGENT_REDIS_ENABLED: "false"
LEAGENT_OBJECT_STORAGE_ENABLED: "false"
```

- [ ] **Step 3: Run YAML parse and static tests**

Run: `uv run pytest tests/test_production_runtime_settings.py -q`

Expected: PASS.

### Task 6: Run final local verification and publish external acceptance gates

**Files:**
- Modify: `deploy/README.md`

- [ ] **Step 1: Run the focused backend suite**

Run: `uv run pytest tests/test_production_runtime_settings.py tests/test_file/test_s3_storage.py tests/test_services/test_redis_runtime.py tests/test_knowledge_answer_grounding.py tests/test_knowledge_quality.py tests/test_file_document_uuid_compat.py -q`

Expected: PASS.

- [ ] **Step 2: Run frontend test/build and backend lint**

Run: `cd frontend && npm run test -- --run src/pages/KnowledgePage/index.test.tsx src/hooks/useKnowledge.test.tsx src/i18n/__tests__/parity.test.ts && npm run build`

Run: `cd backend && uv run ruff check leagent/file/storage leagent/services/cache leagent/services/service_manager.py leagent/knowledge tests/test_production_runtime_settings.py tests/test_knowledge_answer_grounding.py`

Expected: PASS.

- [ ] **Step 3: Document live gates without claiming them passed**

```text
Run an authenticated upload/retrieve/delete cycle against Redis, S3/MinIO,
OpenSearch, Milvus, and reranker; then run cross-Pod resume and WebSocket
delivery tests before increasing replicas.
```
