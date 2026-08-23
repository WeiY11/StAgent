# StAgent Local Semantic Search Implementation Plan

> **For agentic workers:** Execute task-by-task with tests and review gates. Preserve all unrelated dirty-worktree changes.

**Goal:** Add opt-in single-process local SentenceTransformers + sqlite-vec retrieval while keeping BM25/FTS5 authoritative and never downloading a model implicitly.

**Architecture:** Managed local models live under `StAgent_HOME/knowledge/models/`; a lazy embedding provider feeds an independently rebuildable `StAgent_HOME/knowledge/vectors.sqlite3` projection. A backend protocol keeps the existing Milvus path intact. Model changes create a candidate index and switch atomically after a durable vector rebuild task.

**Tech Stack:** Python 3.11, Pydantic v2, SQLModel/SQLite, `sqlite-vec>=0.1.9,<0.2`, optional `sentence-transformers`, FastAPI, React/TypeScript, existing TaskManager.

## Global Constraints

- Preserve the current 124-item dirty worktree; never reset, checkout, or broad-stage unrelated files.
- Local semantic search is opt-in, single-process, and targeted at up to 100,000 chunks; multi-worker deployments use Milvus.
- No default model, no implicit network download, no inline secrets, and no real model smoke test in this implementation.
- BM25/FTS5 remains usable during missing dependencies, model failures, index rebuilds, and sqlite-vec failures.
- Admin-only mutations; status endpoints must not expose secrets or arbitrary local paths to ordinary users.
- Every new frontend key is added to both zh-CN and en-US bundles.

### Task 1: Managed models and local embedding provider

Create the model registry, staging/import/download validation, model fingerprinting, and lazy SentenceTransformers provider. Add unit tests for source validation, no implicit download, prompt routing, dimension checks, and finite vectors.

### Task 2: sqlite-vec backend and backend protocol

Define the vector backend protocol and implement the sidecar sqlite-vec index with versioned vec0 tables, metadata filtering by allowed file IDs, batch upsert/delete/search, restart persistence, and corruption/degraded handling. Keep the Milvus adapter compatible with the protocol. Add storage contract tests.

### Task 3: Configuration, bootstrap, and durable rebuild

Add `knowledge.semantic` config with environment precedence and legacy Milvus compatibility. Wire ServiceManager backend selection and secret-free health status. Add `KNOWLEDGE_VECTOR_REBUILD`, candidate-version lifecycle, dual writes, atomic activation, restart recovery, and BM25 fallback. Add integration tests with a deterministic embedding provider.

### Task 4: API and UI

Add `/api/v1/knowledge/semantic/{status,models,config,rebuild}` plus import/download/delete routes, admin authorization, environment-lock conflicts, task IDs, and safe error payloads. Add Settings → Models local Embedding management and KnowledgePage aggregate status/progress/retry UI with complete i18n parity.

### Task 5: Verification and delivery evidence

Run targeted backend/frontend tests, Ruff, mypy, type-check, build, i18n parity, `git diff --check`, and a deterministic 100,000 × 512 sqlite-vec benchmark. Report real-model integration as unverified and preserve all unrelated changes.
