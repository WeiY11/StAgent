# LeAgent — Docker (minimal)

Single **FastAPI** container with SQLite and local files by default. Set
`DATABASE_URL` to use an external PostgreSQL database. Redis and S3/MinIO are
optional until you scale workers or need shared managed-file storage.

## Prerequisites

- Docker Engine 24+ with Compose v2
- ~2 GB RAM for the image (more if you enable optional OCR build target)

## Quick start

```bash
cd deploy
cp .env.example .env
# Set LEAGENT_SECRET_KEY and at least one LLM API key in .env

docker compose up -d --build
# API: http://localhost:8000/docs
```

Or use the helper:

```bash
./run_docker.sh init
./run_docker.sh up
```

Data layout inside the container:

| Path | Purpose |
|------|---------|
| `/app/data` | `LEAGENT_HOME` — SQLite `leagent.db`, working dirs, secrets |
| `/app/uploads` | Chat attachments (`FILES_UPLOAD_DIR`) |
| `/app/logs` / `/app/cache` | Logs and cache volumes |

## Development overlay

Same image with bind mounts and **uvicorn --reload** (from repo root or `deploy/`):

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build
```

Optional **MailHog** (SMTP capture UI on port 8025):

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml --profile tools up -d
```

## Image build (CI parity)

```bash
cd /path/to/LeAgent   # repository root
docker build -f deploy/Dockerfile -t leagent:local .
```

## Parity with `./start.sh`

For day-to-day development (backend + frontend, `uv`, no Docker DB), prefer the repo root script:

```bash
./start.sh check
./start.sh          # backend + frontend
```

Docker here is for **packaged demos**, **CI**, or hosts where you only want one long-running API process with persisted volumes.

## Kubernetes / Helm

`helm/leagent` contains the repeatable Kubernetes packaging for the API,
health probes, PVCs, secret injection, and optional OpenSearch/Milvus settings:

```bash
helm upgrade --install leagent ./helm/leagent --set existingSecret=leagent-secrets
```

The chart defaults to one replica. PostgreSQL and
`LEAGENT_REDIS_ENABLED=true` are prerequisites before increasing workers or
replicas; the process refuses multi-worker mode without Redis-backed active
run state. The chart does not install Redis, S3/MinIO, OpenSearch, or Milvus.

## Migrations

The container entrypoint runs `python -m leagent.scripts.run_migrations` unless `LEAGENT_RUN_MIGRATIONS=false`. Keep `LEAGENT_MIGRATIONS_REQUIRED=true` in production so startup fails on a failed migration.

SQLite is safe with one worker. If `LEAGENT_WORKERS` is greater than `1`,
configure PostgreSQL and Redis first.

```bash
DATABASE_URL=postgresql+asyncpg://leagent:password@postgres.example.com:5432/leagent
LEAGENT_REDIS_ENABLED=true
LEAGENT_REDIS_URL=redis://redis.example.com:6379/0
LEAGENT_WORKERS=2
```

## Production RAG runtime

The bundled production image includes the backend `production` extra, which
contains the Redis, S3-compatible, and Milvus client libraries. For a
non-Docker installation, run `uv sync --extra production` before enabling
either shared runtime adapter or Milvus-backed retrieval.

Set `LEAGENT_OBJECT_STORAGE_ENABLED=true`, a bucket, and (for MinIO) an
endpoint URL to move managed file blobs to S3-compatible storage. Credentials
can come from the environment, Docker secret injection, or cloud workload
identity. When Redis is enabled, startup pings both its cache and active-run
clients; when object storage is enabled, file-service initialization fails
instead of silently using a local directory.

Before raising replicas, run an authenticated upload/retrieve/delete cycle
against S3/MinIO, a cross-worker pause/resume cycle through Redis, and an
end-to-end RAG query against OpenSearch, Milvus, and the reranker if enabled.

Set `LEAGENT_KNOWLEDGE_QUERY_REWRITE_ENABLED=true` to use the configured fast
LLM route before retrieval. For DeepSeek, LeAgent disables thinking for this
small JSON-only request so completion tokens remain available for the rewrite.
DeepSeek can power query rewrite and answer generation, but not embeddings;
Milvus still requires a separately configured embedding provider and a live
vector smoke test. Enable the semantic plane with all of the following values:

```bash
LEAGENT_KNOWLEDGE_MILVUS_ENABLED=true
LEAGENT_KNOWLEDGE_EMBEDDING_ENABLED=true
LEAGENT_KNOWLEDGE_EMBEDDING_PROVIDER=ollama
LEAGENT_KNOWLEDGE_EMBEDDING_MODEL=nomic-embed-text
LEAGENT_KNOWLEDGE_EMBEDDING_DIMENSION=768
```

The provider and model must be explicit. If either is absent, unregistered, or
lacks embedding support, LeAgent leaves the vector index unset and keeps
SQLite/BM25 retrieval active. The detailed health response reports a safe
reason code such as `embedding_route_unconfigured` or
`provider_lacks_embeddings`; it never exposes credentials or endpoint URLs.

### Rebuild after an index-version change

`LEAGENT_KNOWLEDGE_INDEX_VERSION` is an immutable projection label (letters,
numbers, `.`, `_`, and `-`, up to 64 characters). Change it when the embedding
model, vector dimension, chunking contract, or retrieval schema changes; do
not overwrite the previous projection in place. After deployment, invoke the
authenticated `POST /api/v1/knowledge/reindex-stale` endpoint with a bounded
body such as `{"limit": 100}`. Repeat until `matched` is zero, or until only
the returned `unavailable` sources remain; repair those sources before retrying.
It queues only writable, stale documents and marks unavailable sources as failed. Poll
`GET /api/v1/knowledge/index-status`, then run the retrieval evaluation gate
before directing traffic to the new version.

## Backup / restore

`./run_docker.sh backup` and `restore` only cover the SQLite volume. For PostgreSQL, use `pg_dump` / `pg_restore` against the database server.

```bash
./run_docker.sh backup
./run_docker.sh restore --file ./backups/leagent-YYYY-mm-dd_HHMMSS.db
```
