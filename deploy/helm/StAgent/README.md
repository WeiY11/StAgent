# StAgent Helm chart

This chart packages the API as a Kubernetes Deployment with probes, PVCs,
secret injection, optional ingress, and the current OpenSearch/Milvus
retrieval settings.

The default is deliberately one replica and one worker. PostgreSQL, Redis,
S3/MinIO, OpenSearch, and Milvus are external services; the chart does not
install them. Supply `existingSecret` (or explicitly set
`secret.create=true` with `secret.data.StAgent_SECRET_KEY`) before deployment.
Before increasing `replicaCount`, provide PostgreSQL and set
`env.StAgent_REDIS_ENABLED=true`; the application rejects multi-worker mode
without Redis-backed active-run state. Redis does not make the in-process
event bus or task dispatch shared, so complete a cross-pod resume, WebSocket,
and task smoke test before treating the deployment as HA.

```powershell
helm upgrade --install StAgent .\deploy\helm\StAgent `
  --set image.repository=registry.example.com/StAgent `
  --set image.tag=1.2.7 `
  --set existingSecret=StAgent-secrets
```

Set `StAgent_KNOWLEDGE_OPENSEARCH_URL` and
`StAgent_KNOWLEDGE_MILVUS_ENABLED=true` only after their live health and
retrieval smoke tests pass. The local SQLite/BM25 path remains the safe
fallback when those values are empty.

`env.StAgent_KNOWLEDGE_QUERY_REWRITE_ENABLED=true` enables the configured fast
LLM route before retrieval. DeepSeek is supported for this bounded rewrite
call (thinking is disabled automatically), but it is not an embedding backend;
enable the semantic plane only with an explicit provider/model pair, for
example `env.StAgent_KNOWLEDGE_EMBEDDING_PROVIDER=ollama` and
`env.StAgent_KNOWLEDGE_EMBEDDING_MODEL=nomic-embed-text`, together with
`env.StAgent_KNOWLEDGE_EMBEDDING_ENABLED=true`. If that provider is absent or
does not advertise embedding support, StAgent keeps the vector index disabled
and continues using SQLite/BM25 until a live smoke test is completed.

For S3/MinIO managed files, set the non-secret endpoint/bucket values under
`env`, then add `StAgent_OBJECT_STORAGE_ACCESS_KEY_ID`,
`StAgent_OBJECT_STORAGE_SECRET_ACCESS_KEY`, and optionally
`StAgent_OBJECT_STORAGE_SESSION_TOKEN` to `existingSecret`. IAM/workload
identity deployments may omit those keys. Add `StAgent_REDIS_URL` to the same
secret when Redis is enabled. Keep `StAgent_KNOWLEDGE_OPENSEARCH_API_KEY`,
`StAgent_KNOWLEDGE_MILVUS_URI`, `StAgent_KNOWLEDGE_MILVUS_TOKEN`, and
`StAgent_KNOWLEDGE_RERANK_API_KEY` in that Secret when the corresponding
backend needs them. The image must include the backend `production` extra
(`redis`, `boto3`, and `pymilvus`); the repository Dockerfile does so by
default.

When changing `env.StAgent_KNOWLEDGE_INDEX_VERSION`, deploy the new value,
then call the authenticated `POST /api/v1/knowledge/reindex-stale` endpoint in
bounded batches (for example `{"limit": 100}`) until it reports no stale
documents, or only unavailable sources remain after remediation. Check
`/api/v1/knowledge/index-status` and run the retrieval evaluation gate before
treating the new projection as ready.
