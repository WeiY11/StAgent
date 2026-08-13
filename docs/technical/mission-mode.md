# Mission Mode — Durable Goal-Scoped Autonomous Execution

> **Status:** incubating — P1 skeleton landed (persistence + runner state
> machine); kernel wiring, verifiers, LLM planner, API, and UI pending.
> **Package:** `leagent.mission` · **Persistence:** `agent_missions` /
> `agent_mission_steps` (migration `0023_agent_missions`)

中文版: [`mission-mode_zh.md`](./mission-mode_zh.md)

## Overview

Mission mode gives LeAgent a *persistent, goal-scoped execution layer* for
long-horizon tasks: the user supplies a goal, the kernel produces a durable
task graph (with dependencies), executes it step by step through bounded
context windows (*segments*), gates every step through a verification gate,
continues automatically when a segment exhausts its budget, and distills
outcome experience back into procedural memory.

It is an **additive layer over the existing kernel** — every step still runs
through the canonical `run_loop` (see
[`execution-topology.md`](execution-topology.md)); no kernel contract changes.

## Motivation (the three gaps)

| # | Gap | Evidence |
|---|-----|----------|
| 1 | No executable task-plan layer | Plan-Execute is dormant (`backend/leagent/agent/controller.py` — "Dormant … not wired"); the dependency graph / topological sort / wave scheduling / replan helpers in `backend/leagent/agent/planner.py` are dead code; `todo_write` is a flat, engine-unaware list (`backend/leagent/tools/util/plan_tools.py`) |
| 2 | No enforced verification loop | Agent-side verification is prompt-driven (`backend/leagent/prompts/templates/coding_agent.md`) plus a post-hoc heuristic that only *flags* `partial` (`backend/leagent/agent/subagent.py` — `assess_verification_gap`); `recovery.py` retries tools only; workflow `QualityGate`/`IterativeRefine` live in a separate engine |
| 3 | No autonomous continuation / experience reuse | Checkpoints are saved only on pause reasons and resumed externally (`backend/leagent/sdk/kernel/loop.py`); `max_turns` / token exhaustion is terminal; background agent tasks disable memory (`backend/leagent/tasks/handlers/agent_handler.py`); procedural memory distills success only (`backend/leagent/memory/formation.py`) |

## Goals / Non-Goals

**Goals**

- Run 8–20-step chains across multiple context windows with zero human
  intervention within budget.
- Every step must pass a verification gate before it counts as done.
- Budget exhaustion, process restart, and step failure are recoverable —
  pause, adjust, resume; no replay from scratch.
- Success *and* failure distill into recallable procedural experience.

**Non-Goals**

- Not a replacement for the workflow DAG engine (deterministic pipelines
  still go through `WorkflowExecutor`).
- No multi-user collaboration / cross-device sync.
- No mission-nested-mission (recursion depth 1).
- No change to existing single-turn chat behavior (mission is an overlay).

**Success metrics:** mission completion rate, unattended-completion share,
steps intercepted-and-fixed by the verification gate, human takeovers per
mission.

## Core Concepts

| Concept | Definition |
|---|---|
| **Mission** | One durable execution unit for one user goal (row in `agent_missions`) |
| **MissionPlan** | Validated dependency DAG: `steps[]` with `depends_on`, `kind`, plus `expected_output` |
| **Step** | One plan node; state machine `PENDING → RUNNING → VERIFYING → PASSED / FAILED / SKIPPED / BLOCKED`, plus `WAITING_APPROVAL` in `step_confirm` mode |
| **Segment** | One bounded `run_loop` invocation for a step — a fresh context window; mission context is injected as a compact prompt, history is *not* replayed |
| **Verification gate** | Mandatory check before a step passes: deterministic checks by `kind`, optionally an LLM critic; failures feed bounded retry |
| **Continuation** | When a segment ends on a budget reason, a continuer builds a fresh prompt from the step summary and a new segment continues the same step |
| **Replan** | On step failure, the planner may emit a revised plan for the remaining work, written under `plan_id + 1` (old rows kept for audit) |

## Architecture

```
Ingress (Chat / Cron / SDK / Task) ──► MissionService (create, persist)
                                              │
                                              ▼
                               Task queue (task_type=MISSION, recover_pending_jobs)
                                              │
                                              ▼
                        ┌── MissionRunner (leagent/mission/runner.py) ──┐
                        │  MissionPlanner  ── plan / replan (DAG)        │
                        │  wave scheduling ── persisted wave column      │
                        │  SegmentExecutor ── one run_loop per segment   │
                        │  MissionVerifier ── verification gate          │
                        │  MissionContinuer ── bounded continuation      │
                        └───────────────┬───────────────────────────────┘
                                        │ state + events
                ┌───────────────────────┼──────────────────────┐
                ▼                       ▼                      ▼
        MissionRepository        CheckpointStore         EventManager
        (agent_missions,         (per-segment,            (mission_* events +
         agent_mission_steps)     existing SQL store)      ExecutionRun scope)
```

### File map

```
backend/leagent/mission/
├── __init__.py       # public exports
├── types.py          # MissionBudget / MissionUsage / MissionPlan / MissionStepState /
│                     # SegmentResult / VerificationResult (DB-agnostic working shapes)
└── runner.py         # MissionRunner state machine + seam protocols + P1 stubs
                      # (StaticPlanner / AlwaysPassVerifier / IdentityContinuer)

backend/leagent/db/
├── models/mission.py           # Mission / MissionStep SQLModel + enums
├── repositories/missions.py    # MissionRepository (protocol + Db implementation)
└── (models/__init__.py, repositories/__init__.py wiring)

backend/leagent/alembic/versions/0023_agent_missions.py
backend/leagent/db/models/task.py        # TaskType.MISSION (id prefix "n")
backend/leagent/db/sqlite_compat.py      # PARENT_ID_TABLES += agent_missions
```

### Layering rules

- Persistence lives in `leagent/db` (canonical); `leagent/mission` never
  constructs engines/sessions itself.
- The runner depends on injected seams (`MissionPlanner`, `SegmentExecutor`,
  `MissionVerifier`, `MissionContinuer`) — kernel wiring happens in the
  integration layer, keeping `leagent/mission` unit-testable without an LLM.
- Every step still flows through `run_loop` via `SegmentExecutor`; the
  runner never drives a `QueryEngine` directly.

## Data Model

```sql
agent_missions (
  id              UUID PK,           -- BaseModel (UUIDMixin + TimestampMixin)
  user_id         UUID NULL FK users.id,      -- indexed
  session_id      UUID NULL,                 -- indexed (chat-originated)
  goal            TEXT NOT NULL,             -- user goal (max 4000)
  agent_id        VARCHAR(100) NULL,
  status          VARCHAR(64) NOT NULL,      -- planning|ready|running|paused|completed|failed|aborted|expired
  autonomy        VARCHAR(64) NOT NULL,      -- full|step_confirm|readonly
  verify_level    VARCHAR(64) NOT NULL,      -- none|heuristic|llm_critic
  expected_output TEXT NULL,
  plan_id         INT NOT NULL DEFAULT 0,    -- current plan version
  budget_json     TEXT NULL,                 -- MissionBudget serialized
  usage_json      TEXT NULL,                 -- MissionUsage ledger
  pause_reason    VARCHAR(200) NULL,
  error           TEXT NULL,
  started_at / completed_at DATETIME NULL,
  created_at / updated_at DATETIME NOT NULL
);

agent_mission_steps (
  id              UUID PK,
  mission_id      UUID NOT NULL FK agent_missions.id,   -- indexed
  step_no         INT NOT NULL,            -- stable within the plan
  plan_id         INT NOT NULL DEFAULT 0,  -- plan version (replan = +1)
  description     VARCHAR(4000) NOT NULL,
  kind            VARCHAR(64) NOT NULL,    -- code|research|data|file|generic
  depends_on      TEXT NULL,               -- JSON [step_no, …]
  wave            INT NOT NULL DEFAULT 0,  -- scheduling wave (persisted)
  status          VARCHAR(64) NOT NULL,    -- pending|running|verifying|waiting_approval|passed|failed|skipped|blocked
  attempt         INT NOT NULL DEFAULT 0,  -- segments consumed (incl. verify-fix)
  verify_result   TEXT NULL,               -- JSON {passed, feedback, evidence}
  segment_summary TEXT NULL,               -- cross-segment memory carrier
  checkpoint_id   VARCHAR(200) NULL,       -- last pause checkpoint (audit)
  error           TEXT NULL,
  started_at / completed_at DATETIME NULL,
  UNIQUE (mission_id, plan_id, step_no)
);
```

**State ownership** (extends the table in `execution-topology.md`):

| State | Owner | Durable |
|---|---|---|
| Mission plan + step working state | `MissionRepository` (`agent_missions` / `agent_mission_steps`) | Yes |
| Mission segment pause | existing `CheckpointStore` (one checkpoint per segment) | Yes (SQL) |

### Plan versioning (deliberate design)

The steps table holds the working state of the **current** plan version
(`plan_id`). A replan writes new rows under `plan_id + 1`; old rows remain
for audit and for "what failed and why" reconstruction. There is **no**
separate `plan_json` column on the mission — the steps table is the single
source of truth (avoids dual-write drift).

### Scheduling (wave persistence)

Wave numbers are computed once by `MissionPlan.waves()` (reusing
`get_parallel_groups` from `backend/leagent/agent/planner.py`) and
persisted in the `wave` column at plan time. At run time, the runner takes
the remaining steps of the **lowest wave** as the ready set — no graph
recomputation, and no false "unknown dependency" errors on steps whose
upstreams are already terminal.

## Runner State Machine

```
CREATE → PLANNING ──plan──► READY ──run──► RUNNING ──► COMPLETED
                 (invalid plan → FAILED)      │  ▲
                       (step_confirm)         │  │ resume / approve
                                             ▼  │
                                PAUSED ◄── budget / step_confirm /
                                             verification_failed / user / awaiting_user_input
                                             FAILED / ABORTED / EXPIRED (deadline)
```

### Public API (`MissionRunner`)

| Method | Transition | Notes |
|---|---|---|
| `start(mission)` | `PLANNING → READY / FAILED` | generates plan via `MissionPlanner`, validates, persists steps |
| `run(mission)` | → `COMPLETED / FAILED / EXPIRED / PAUSED` | idempotent; plans `PLANNING`, resumes `PAUSED`, recovers stuck steps |
| `pause(mission, reason)` | → `PAUSED` | graceful stop at current step end |
| `resume(mission)` | `PAUSED → RUNNING` | |
| `abort(mission)` | → `ABORTED` | terminal |
| `approve_step(mission, step_no, approved, note)` | `WAITING_APPROVAL → PASSED / FAILED` | `step_confirm` mode; auto-resumes |

### Mission loop (per wave)

1. **Load** remaining steps of the current plan; if none → `COMPLETED`.
2. **Block check** — a `WAITING_APPROVAL` step pauses the mission.
3. **Ready set** — remaining steps at the lowest `wave`.
4. **Per step** (serial in P1; parallel waves in P2):
   - build the step prompt (goal + expected output + upstream `segment_summary`s + step description),
   - run segments up to `max_segments_per_step`:
     - segment succeeds → `VERIFYING` → gate → passed / retry with feedback / fail;
     - `result.continuable` (budget reason) → continuer prompt → next segment (event `mission_segment_continued`);
     - `awaiting_user_input` → persist checkpoint, `PAUSED`;
     - `aborted` → `ABORTED`;
   - on segment-budget exhaustion → step `FAILED` ("segment budget exhausted");
   - on verification failure past `max_verify_retries` → step `FAILED`;
   - on step `FAILED` → one bounded replan (`MissionPlanner.replan`, optional seam);
     without replanner / invalid replan → `PAUSED(reason=step_failed)`.

### Continuation (`MissionContinuer`)

When a segment ends with `max_turns` / `token_budget_exceeded` /
`prompt_too_long` (`CONTINUABLE_SEGMENT_REASONS` in `leagent/mission/types.py`,
mirroring the kernel's `RESUMABLE_CHECKPOINT_REASONS`), the runner saves the
checkpoint (audit) and issues a **fresh segment** with a continuation prompt
instead of replaying history. The finished segment's text is truncated into
`segment_summary` (4 KB) — the cross-segment memory carrier for downstream
steps.

### Crash recovery

`run()` resets steps stuck in `RUNNING` / `VERIFYING` (a crash mid-segment)
to `PENDING` before looping. Mission liveness rides the task queue
(`task_type=MISSION`) so `recover_pending_jobs` re-queues after a process
restart.

## Verification Gate

`MissionVerifier.verify(mission, step, result) → VerificationResult{passed, feedback, evidence}`.
P1 ships `AlwaysPassVerifier` (stub). Planned levels by `kind`:

| `kind` | Heuristic (P1/P2) | LLM critic (`verify_level=llm_critic`) |
|---|---|---|
| `code` | re-run the step's test/build commands (generalizes `subagent.assess_verification_gap`) | read-only critic sub-agent |
| `file` / `data` | `produced_files` existence, schema checks | — |
| `research` / `generic` | — | read-only critic sub-agent |

Failed verification feeds the feedback back into a bounded retry segment
(`attempt` grows; `max_verify_retries` bounds *verification* retries, while
`max_segments_per_step` bounds *total* segments including verify-fix runs).
`step_confirm` mode additionally pauses after verification for a human
verdict (`approve_step`).

## Budgets, Autonomy, Security

| Dimension | Rules |
|---|---|
| **Budget** (`MissionBudget`, JSON on mission) | `max_steps=20`, `max_segments_per_step=3`, `max_verify_retries=2`, `max_total_tokens=4_000_000`, `max_cost_usd` (optional), `deadline` (optional → `EXPIRED`). Any cap hit → `PAUSED` (never auto-raise; resume may patch) |
| **Autonomy** | `full` (default, unattended within budget) / `step_confirm` (per-step human approval) / `readonly` (read-only tool whitelist — P2) |
| **Human gate** | `human_gate` policy rules remain in force regardless of autonomy — mission mode never bypasses existing gates |
| **Concurrency** | P1 serial; P2 parallel waves bounded by `max_concurrency_per_user`; multi-worker requires PostgreSQL + sticky sessions (per `execution-topology.md`) |
| **Memory** | mission runs enable memory formation (unlike `agent_handler.py`), behind a `mission_memory_enabled` switch |

## API Design (planned)

```
POST   /api/v1/missions                 {goal, agent_id?, autonomy, verify_level, budget?} → mission
GET    /api/v1/missions                 ?status=&limit=
GET    /api/v1/missions/{id}            → plan graph, step states, verify evidence, log tail
GET    /api/v1/missions/{id}/events     → SSE (mission_* events)
POST   /api/v1/missions/{id}/pause      {reason?}
POST   /api/v1/missions/{id}/resume     {budget_patch?}
POST   /api/v1/missions/{id}/abort
POST   /api/v1/missions/{id}/steps/{no}/approve   {verdict, note}
```

Trigger entry points: chat (`/mission <goal>` or button), SDK
(`AgentRuntime.run_mission`, `leagent.sdk.__version__` → 0.2.0), Cron /
background task (`task_type=MISSION`).

## Frontend Design (planned)

- New page `pages/Missions/` — list + detail. Detail: plan timeline (P1) /
  ReactFlow dependency graph preview (P2), step status badges, verification
  evidence, budget usage bar, live log tail, pause/resume/abort/approve.
- Chat: compact `MissionCard` (progress + current step) inserted into the
  stream when a mission starts.
- i18n: new bundle `missions.json` in both `zh-CN/` and `en-US/` (parity
  test enforced).

## Observability

- `mission_*` events on the `EventManager` bus: `mission_created`,
  `mission_planned`, `mission_plan_rejected`, `mission_step_started`,
  `mission_step_verifying`, `mission_step_verified`, `mission_step_verify_failed`,
  `mission_step_passed`, `mission_step_failed`, `mission_step_awaiting_approval`,
  `mission_step_approved`, `mission_segment_continued`, `mission_replanned`,
  `mission_paused`, `mission_resumed`, `mission_completed`, `mission_failed`,
  `mission_aborted`, `mission_expired`. (P2 wiring.)
- `ExecutionRun` scope gains `mission` / `mission_step`; step runs attach via
  `parent_run_id` so OTel spans chain.
- Per-segment checkpoints are retained as audit trail ("which segment hit the
  budget, what summary was injected").

## Testing

| Layer | Coverage |
|---|---|
| Unit (P1, landed) | plan validation + cycle detection; wave grouping; continuation within step budget; budget exhaustion → pause; verify retry then pass / exhausted → fail; step_confirm pause + approve; failure without replanner → pause; replan under plan_id+1; token/deadline caps; stuck-step recovery; abort terminality; store CRUD / upsert idempotence / plan-scoped replace / user listing / cascade delete |
| Integration (planned) | mock-LLM end-to-end 3-step mission; budget exhaustion → auto-continue → complete; process restart via `recover_pending_jobs`; `readonly` whitelist interception; verify-fail → replan → pause path |
| Regression | chat/task paths untouched (`run_loop` contract intact); `test_file/test_invariants.py` green; UUID-compat suite green |

## Implementation Status

| Piece | Status (2026-08-13) |
|---|---|
| `agent_missions` / `agent_mission_steps` + migration `0023` + `PARENT_ID_TABLES` | ✅ landed, upgrade/downgrade verified |
| `MissionRepository` + `Repositories.missions` wiring | ✅ landed |
| `leagent/mission/types.py` (budget/usage/plan/segment/verification shapes) | ✅ landed |
| `MissionRunner` state machine + seam protocols + stubs (`StaticPlanner` / `AlwaysPassVerifier` / `IdentityContinuer`) | ✅ landed |
| Unit tests (`tests/test_mission_store.py`, `tests/test_mission_runner.py` — 22 cases) | ✅ green |
| `TaskType.MISSION` (+ id prefix `n`) | ✅ landed |
| Segment executor wired to `run_loop` (`mission/segment.py`) | ⏳ next |
| LLM planner + replan prompts (`mission/planner.py`) | ⏳ |
| Heuristic verifier (generalize `assess_verification_gap`) | ⏳ |
| `readonly` autonomy enforcement | ⏳ |
| `mission_*` events on EventManager + `ExecutionRun` scope | ⏳ |
| `/api/v1/missions` router + SSE | ⏳ |
| Frontend Missions page + chat MissionCard + i18n | ⏳ |
| Failure-experience distillation into procedural memory | ⏳ (P3) |

## Phased Plan

| Phase | Scope |
|---|---|
| **P1 (MVP, in progress)** | persistence + runner state machine ✅; segment executor → `run_loop`; heuristic verification gate; bounded continuation; REST + SSE; Missions page (timeline); `mission_*` events |
| **P2** | parallel waves (via task queue parent/child tasks, not by opening `AgentTool` concurrency); `step_confirm` / `readonly`; chat MissionCard; cron-triggered missions; ReactFlow plan preview; LLM critic verifier |
| **P3** | failure-experience distillation into procedural memory (new formation weights); budget/cost dashboard; mission templates |

## Risks & Mitigations

| Risk | Mitigation |
|---|---|
| Cost / runtime blowup | mission-level token + cost + deadline caps → `PAUSED`; bounded continuation; no auto-raise |
| Weak LLM plans | small waves, mandatory per-step verification, one bounded replan, `max_steps` cap; optional plan-review sub-agent (P2) |
| Context drift across segments | mission context + upstream `segment_summary` + recall injected every segment |
| SQLite single-writer | P1 serial; P2 bounded parallelism; documented multi-worker prerequisites |
| Memory pollution | formation thresholds + idempotent dedup + kill switch |
| Long-task UX | `mission_*` events → chat card / IM channels (P2); always pause/takeover available |

## Open Questions

1. Should the verification gate accept per-mission custom verify commands
   (e.g. "must be `npm test` green")?
2. Should `verify_level` default to `llm_critic` for `code` goals (costlier,
   more robust) or stay `heuristic`?
3. Should `PLANNING → READY` require a user confirmation independent of
   `autonomy`?

## References

- [`execution-topology.md`](execution-topology.md) — kernel contract, state ownership, run correlation
- [`agent_sdk.md`](agent_sdk.md) — `AgentRuntime` / `run_loop` public surface
- [`agent-runtime.md`](agent-runtime.md) — runtime wiring, `runtime_profile` budgets
- `backend/leagent/agent/planner.py` — dormant planner + scheduling helpers (reused)
- `backend/leagent/db/repositories/missions.py` — repository pattern reference
