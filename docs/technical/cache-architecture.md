# Cache Architecture

StAgent runs with **zero external cache dependencies by default**. Every cache
described here is a **process-local, in-memory** structure that improves latency
and reduces provider/database load. It is never a source of truth and never an
access-control boundary. If the process restarts, every cache is empty and the
system stays correct because the database and file layer are the only durable
state.

> **Redis is not used and not required.** The API deliberately reports
> `backend=memory`, `persistent=false` so operators never assume a shared cache
> exists. See [Redis promotion gate](#9-redis-promotion-gate) for the exact
> conditions under which a shared cache could be justified.

## 1. Cache ownership

| Cache | Owner | Scope | Bound | Durable |
|---|---|---|---|---|
| `CacheService` (application L2) | `ServiceManager.cache` | per-namespace logical keys | `max_items`, `max_value_bytes` | No |
| Session L1 | `TieredSessionStore` | per-session transcript | `memory_l1_max_items` | Backed by `session_state_v1` (SSOT is durable) |
| Workflow output cache | `WorkflowExecutor` (`CacheSet`) | per-execution scope digest (owner/workflow/permission/model) | `cache_options.max_items` (bounded LRU) | No |
| Provider prompt-prefix cache | LLM provider (Anthropic/OpenAI) | provider-side, keyed by stable prefix | provider-managed | No (billing optimization only) |

The provider prompt cache is **outside** this control plane: it is a billing
optimization managed by the model vendor keyed on stable request prefixes. It is
**not** application result caching and **not** an ACL (see the prompt-cache
guide, `docs/guides/agent/18-prompt-cache-and-context-hygiene.md`).

## 2. Read path

```
caller → CacheService.get_or_set(key, factory, namespace=...)
       → L1 lookup (InMemoryCache, sharded LRU)
          ├─ hit  → return cached value (record hit)
          └─ miss → single-flight fill:
                      first caller runs factory (database / provider read)
                      concurrent callers coalesce onto the same in-flight task
                    → bounded write back into L1 (record fill + coalesced count)
```

Reads never invent data: on a miss the factory pulls from the authoritative
store (database, file layer, or provider). Concurrent identical misses run the
factory exactly once (single-flight), and the extra waiters are counted as
`coalesced`.

## 3. Write path

```
1. Durable commit first  — write to the database / file layer (SSOT).
2. Generation bump        — increment the owner/resource generation counter so
                            every key derived from the old generation is now
                            logically stale (no scan, no delete storm).
3. Later TTL cleanup      — the maintenance sweep and LRU eviction reclaim the
                            now-unreachable old-generation entries lazily.
```

A cache entry is only published **after** the durable write succeeds. If the
database write fails, no new entry becomes visible and the next read re-fills
from the authoritative store.

## 4. Key schema

Every key embeds enough scope that two tenants, two resource versions, or two
request variants can never collide:

```
<schema_version>:<owner_scope>:<resource_scope>:<generation>:<variant_digest>
```

| Segment | Meaning | Example |
|---|---|---|
| `schema_version` | cache key format version; bump to invalidate globally | `v1` |
| `owner_scope` | tenant / user isolation | `owner:018f...` |
| `resource_scope` | logical resource | `session:abc`, `workflow:TPL-ART-01` |
| `generation` | monotonic invalidation counter | `gen:42` |
| `variant_digest` | hash of inputs that change the result | `sha256:9f2c...` |

Workflow output keys additionally prefix a **scope digest** derived from
`(owner_id, workflow_id, permission_context, model)` (`WorkflowCacheScope`) so a
saved flow re-run by a different owner never reuses another owner's node output.

## 5. TTL semantics

- **`None`** — no expiry from TTL; the entry only leaves via LRU eviction or a
  generation bump.
- **Zero / negative** — treated as "do not persist with a timed expiry"; passes
  through unchanged (no jitter applied).
- **Jitter** — a deterministic, hash-derived `+/- ttl_jitter_ratio` band
  (default `0.05`) is applied per key so a namespace's entries do not all expire
  in one burst (stampede protection). Determinism keeps tests repeatable.
- **Maintenance sweep** — a background loop (`maintenance_interval_sec`, default
  `30s`, `0` disables) sweeps expired entries so TTL memory is reclaimed even
  when a key is never read again. Expirations are counted.

Per-namespace TTLs (mutable data short, stable data long) are the primary lever
for aggregate hit rate — see `namespace_ttls`.

## 6. Values that must never be cached

- Permission / RBAC decisions, approval outcomes, human-gate results.
- Secrets, API keys, JWTs, one-time download credentials, verification codes.
- Full LLM conversation responses (sampling, tool state and safety policy make
  reuse riskier than the saving).
- Any value larger than `max_value_bytes` (default 1 MiB) — rejected, not
  cached, so a few large payloads cannot evict the whole working set. Large
  blobs cache a `FileRef` / `MediaRef` instead.

## 7. Metrics and alerts

Exposed via `/api/v1/stats/cache` and Prometheus (label: `cache_name` only —
strictly low cardinality; owner/resource/session/model never become labels):

| Metric | Meaning | Alert intent |
|---|---|---|
| recent + cumulative hit rate | sliding-window and lifetime hit ratio | sudden hit-rate drop → key churn / prefix instability |
| `cache_fill_duration_seconds` | factory (database/provider) fill latency | rising fills → slow upstream |
| `cache_coalesced_total` | waiters merged onto one fill | high coalescing → hot key / stampede pressure absorbed |
| `cache_evictions_total` | LRU evictions | sustained evictions → undersized `max_items` |
| `cache_expirations_total` | TTL expirations | expected background churn |
| `cache_rejections_total` | oversized-value rejections | frequent rejections → caller caching too-large payloads |
| resident size | current item count vs `max_items` | approaching cap → capacity review |

## 8. Failure behavior and rollback

- **Cache failure degrades to correctness, never business failure.** Any cache
  miss, eviction, or full flush falls back to the database/provider read; the
  business result stays correct, only latency changes.
- **Rollback levers:** set the workflow cache mode to `none`; shorten
  `namespace_ttls` to near-zero; lower `max_items`. If correctness is ever
  questioned, stop wiring domain consumers while keeping database reads intact —
  the system runs identically, just slower.

## 9. Redis promotion gate

A shared/external cache (e.g. Redis) is justified **only** when *all* of the
following are measured to be true — not before:

1. Persistence has moved to **PostgreSQL** (`DATABASE_URL`), and
2. the deployment runs **multiple workers / processes** (not the default single
   SQLite writer), and
3. profiling shows **measurable cross-worker cache misses** that materially hurt
   latency or provider cost (each worker warming its own process-local cache).

Until all three hold, a shared cache adds an external dependency and a new
failure mode without a proven benefit. This document does **not** claim Redis
exists or is wired.

## 10. Proof boundary

Unit and integration tests prove **invariants** (single-flight, bounded resident
set, owner isolation, no publication after a failed durable write, oversized
rejection). They do **not** prove a production hit rate. Hit-rate targets must be
established from a real request-distribution baseline; a green test suite or a
local benchmark (`tests/bench_cache_hot_paths.py`) is local evidence only and is
never sufficient on its own to gate a rollout.
