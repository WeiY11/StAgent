# LeAgent Execution Topology

> Status: living contract. See also [`agent_sdk.md`](agent_sdk.md) and
> [`agent-runtime.md`](agent-runtime.md).

中文版: [`execution-topology_zh.md`](./execution-topology_zh.md)

This document formalizes how requests flow through LeAgent's execution stack,
which subsystems own state, and the canonical entry paths every caller should use.

## Layer Model

```
Ingress (HTTP/SSE/WS/Task/Cron)
        │
        ▼
Facade (ServiceManager.runtime_context, AgentRuntime, WorkflowService)
        │
        ▼
Kernel (run_loop → QueryEngine → query → ToolExecutor)
        │
        ▼
Durable state (SessionState, CheckpointStore, WorkflowStateStore)
        │
        ▼
Observability (EventManager, OTel)
```

## Canonical Agent Path

All agent turns **must** flow through `leagent.sdk.kernel.loop.run_loop`, either
directly or via `AgentRuntime.stream()` / `AgentController._run_via_query_engine()`.

| Caller | Entry | Kernel |
|--------|-------|--------|
| Chat SSE | `AgentController.run_stream` | `run_loop` |
| SDK | `AgentRuntime.stream` | `run_loop` |
| Background task | `AgentTaskHandler.spawn` | `AgentRuntime.stream` → `run_loop` |
| Sub-agent | `subagent._run_engine` | `run_loop` |
| Workflow agent node | `agent_exec.run_agent_node` | `AgentRuntime.stream` → `run_loop` |

Direct `QueryEngine.submit_message()` is reserved for tests and kernel internals.

## Runtime Wiring

Use `ServiceManager.runtime_context` (lazy singleton) as the single factory for:

- `ToolRegistry` / `ToolExecutor`
- `HookManager` + default hooks
- `CheckpointStore` (SQL when DB present)
- `SessionManager`, `AgentMemory`, `LLMService`

`build_agent_controller()` and workflow bootstrap consume this bundle.

## Workflow Orchestration Models

| Model | Schema | Executor | When to use |
|-------|--------|----------|-------------|
| **DAG engine** | `WorkflowDocument` | `WorkflowExecutor` | Saved flows, cron, agent `workflow_run`, agent nodes |
| **Chat step card** | `ChatWorkflowSpec` → compiled linear flow | `WorkflowService` scoped run | Playbook steps in chat |
| **Chat embed** | Validated Flow JSON | Preview + Flow API | Graph preview in chat |

Chat step cards compile to linear `WorkflowDocument` instances via
`leagent.chat_workflow.compile` so step execution shares the engine kernel.

## State Ownership

| State | Owner | Durable |
|-------|-------|---------|
| Chat transcript SSOT | `TieredSessionStore` (`session_state_v1`) | Yes |
| Agent turn pause | `CheckpointStore` (`agent_checkpoints`) | Yes (SQL) |
| Agent running trace | `TraceStore` (`agent_traces` / spans) | Yes (SQL) |
| Workflow run | `WorkflowStateStore` | Yes (SQL) |
| Chat step results | `Message.extensions.chat_workflow_step_runs` | Yes |
| Background task log | `TaskManager` output file | Yes |

## Pause / Resume

Unified `PauseToken` (`leagent.runtime.execution_run`) references:

- `checkpoint_id` for agent-scope pauses
- `workflow_execution_id` + `workflow_state_id` for DAG pauses
- `scope`: `chat_turn` | `workflow` | `task` | `tool_only`

Chat resume: `POST /chat/sessions/{id}/resume-checkpoint` + stream with `checkpoint_id`.
Workflow resume: `POST /workflow/prompts/{id}/resume`.

## Observability

Lifecycle events publish through `EventManager` (`FLOW_*`, `TASK_*`, `AGENT_*`):

- `EventManager.bridge_workflow_progress_event()` — workflow executor → `FLOW_*`
- `EventManager.publish_agent_lifecycle()` — chat SSE completion traces
- `EventManager.publish_flow_lifecycle()` — flow runs with `run_id` / `parent_run_id`
- `EventManager.emit_task_event()` — background task lifecycle

Workflow WebSocket and chat SSE remain transport layers; webhooks subscribe via `EventManager`.

OTel spans link via `run_id` and `parent_run_id` on `ExecutionRun`.

## Workflow Start API

Use `WorkflowService.start(trigger=...)` for all flow runs:

- HTTP `/workflow/prompts`, `/workflow/flows/{id}/run`
- Agent tool `workflow_run`
- Cron jobs
- Subworkflow nodes

Trigger metadata records `manual`, `agent`, `cron`, `chat_step`, or `subworkflow`.

## Unified Execution Plane

All ingress surfaces mint an `ExecutionRun` via `leagent.runtime.execution_factory`
and publish lifecycle signals through `EventManager` with shared correlation keys.

```
Ingress (Chat SSE / Chat step HTTP / Workflow WS / Task / GenUI / Cron)
        │
        ▼
ExecutionRun registry (run_id, parent_run_id, scope, prompt_id)
        │
        ├──► Facade (AgentRuntime, WorkflowService.run_compiled_document)
        │         │
        │         ▼
        │    Kernel (run_loop → QueryEngine → ToolExecutor)
        │
        ▼
Durable state (SessionState, CheckpointStore, WorkflowStateStore, step_runs)
        │
        ▼
Observability (EventManager FLOW_*/TASK_*/AGENT_*, OTel, WS/SSE transports)
```

### Correlation keys

| Key | Purpose |
|-----|---------|
| `run_id` | Primary execution unit identifier; set on `Event.correlation_id` |
| `parent_run_id` | Links child runs (workflow step, sub-agent, task) to parent chat turn |
| `session_id` | Chat session scope for timeline hydration |
| `prompt_id` | Workflow WebSocket subscription key (`/workflow/ws/executions/{prompt_id}`) |
| `task_id` | Background task queue identifier |

### Single run owner rule

One `ExecutionRun` is minted per chat turn in `run_agent_stream`. The `run_id`
is passed into `AgentController` via `tool_extra["run_id"]`. Child scopes
(workflow steps, sub-agents, background tasks) register with
`parent_run_id` pointing at the chat turn or task run.

### Chat-step WebSocket bridge

Chat playbook steps compile via `leagent.chat_workflow.compile` and execute through
`WorkflowService.run_compiled_document()` (not direct `_executor.execute_async`).

Each step run:

1. Creates a `WorkflowExecution` row (`flow_id=null`, `trigger_type=chat_step`)
2. Registers `ExecutionRun(scope=workflow, parent_run_id=…, prompt_id=…)`
3. Persists `prompt_id` and `run_id` in `Message.extensions.chat_workflow_step_runs`
4. Publishes progress via the same WebSocket stream as editor runs

The chat frontend subscribes to `/workflow/ws/executions/{prompt_id}` for live
node progress identical to the workflow editor overlay.

### Execution API

- `GET /chat/sessions/{id}/executions` — active/blocked in-process runs for timeline hydration (single-worker; not durable across processes)
- Chat SSE emits additive `execution_started` with `{ run_id, session_id, scope }`

### Single-process registry note

`ExecutionRunRegistry` is the default, in-process implementation of the
`RunStore` protocol (`leagent.runtime.run_store`). It is a per-process singleton
reached everywhere through `get_run_store()`; blocked runs are retained in it
until resume or explicit `end_execution`, and a throttled, TTL-based lazy sweep
(`LEAGENT_EXECUTION_RUN_TTL_SECONDS`, default 6h; set `0` to disable) reclaims
abandoned pauses so a long-lived single-process server stays bounded.

#### Scaling to multiple workers

The default single-worker / SQLite deployment is unchanged: `get_run_store()`
returns the in-process registry and nothing touches Redis.

Because the registry is process-local, a plain multi-worker deployment must
either (a) pin each session to one worker (**sticky sessions**), or (b) install
a **shared run store** at startup so cross-scope linking and pause/resume lookup
work when a chat turn, its sub-agents and its workflow steps land on different
workers.

`RunStore` is the seam for (b). `RedisRunStore`
(`leagent.runtime.redis_run_store`) is a drop-in shared backend:

- Build it from a **synchronous** redis client and install it once at process
  startup: `set_run_store(build_redis_run_store(redis_client))`. The
  `RunStore` contract is synchronous because runs are minted/retired from both
  sync and async hot paths.
- Runs are serialized via `ExecutionRun.to_dict()`/`from_dict()`. Ordering for
  `get_active_chat_turn` uses the wall-clock `created_at` (comparable across
  workers), not the process-local monotonic `registered_at`.
- Every key carries a TTL mirroring the in-process sweep so an abandoned pause
  cannot leak.
- Callers that **mutate** a fetched run (e.g. `run.pause(...)` in the SSE loop
  and the workflow executor) re-register it so the shared store observes the
  change; this is a no-op re-store for the in-process backend.

**Prerequisites / thresholds:** adopt a shared run store only when the
deployment actually runs `LEAGENT_WORKERS > 1` (or multiple processes) **and**
cannot use sticky routing. It requires a reachable Redis (not provisioned in the
standalone build, where `ServiceManager.redis` is `None`) and the `redis`
client dependency. Below that threshold the in-process registry is strictly
preferred — it avoids per-run network round-trips on the hot path.

**WebSocket fan-out is a separate concern.** `DistributedConnectionManager`
(`apps/gateway/infrastructure/ws_fanout.py`) is in-memory only today, so live
WebSocket/SSE event delivery still assumes a client's socket and its run share a
worker. A shared `RunStore` fixes *run bookkeeping* across workers but does not
by itself fan out live events; multi-worker event delivery additionally needs a
Redis pub/sub backend behind `DistributedConnectionManager.attach_redis` (or
sticky sessions).

**Explicit boundary (do not overstate).**

- The in-process registry is the default; nothing here changes the SQLite +
  single-worker deployment.
- `RedisRunStore` is an **opt-in active-run metadata seam** only.
- It does **not** make the `EventManager`, WebSocket fan-out, task ownership /
  leases, session-store writes, or the in-process caches distributed.
- **PostgreSQL + sticky sessions is the supported interim multi-worker
  boundary.** Installing `RedisRunStore` alone does not make multi-worker
  operation complete; a full shared EventBus, task lease and session
  concurrency-control design is a separate, future stage.

