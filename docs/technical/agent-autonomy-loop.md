# Agent Autonomy Loop — Reflective Recovery · Lesson Memory · Parallel Crew

> **Status:** proposal (design review) — no code landed yet.
> **Packages:** `StAgent.agent.recovery` (extended) · `StAgent.memory` (extended) ·
> `StAgent.tools` (new `crew_fork`) · `StAgent/agent/reflection.py` (new)

中文版: [`agent-autonomy-loop_zh.md`](./agent-autonomy-loop_zh.md)

## 1. Overview

The Autonomy Loop is a **program of three kernel-level modules** that close
the gap between "the agent tried something and it failed" and "the agent got
better without a human". Together with [Mission Mode](mission-mode.md) they
complete one full autonomy stack:

```
Mission Mode   ── 规划层: 目标 → 持久任务图 → 分步执行 → 验证门
Module A       ── 执行修复层: 工具失败 → 反思分类 → 有界纠正(不再靠 LLM 自觉)
Module B       ── 经验层: 失败经历 → 蒸馏教训 → 跨会话召回(不再只记成功)
Module C       ── 并行放大层: 一个 turn 扇出 N 个子代理,共享工件,归并结果
```

Design principles are inherited from Mission Mode:

- **Additive seams, kernel untouched** — every module plugs into an existing
  extension point (`_dispatch_tools` middleware, memory formation, tool layer);
  the canonical `run_loop` contract does not change.
- **Bounded everywhere** — reflection, distillation, and fan-out all carry
  hard caps; nothing can grow without limit.
- **Graceful degradation** — every module disabled leaves today's behavior
  exactly as-is.

---

## 2. Module A — Reflective Recovery Loop (反思式自纠错)

### 2.1 Motivation & evidence

Today's recovery is purely mechanical and only covers two failure classes:

| Handled | Evidence | Not handled |
|---|---|---|
| Timeout → one rerun with doubled timeout | `backend/StAgent/agent/recovery.py` `_handle_timeout` | parameter mistakes, missing context, env/permission blocks |
| Rate limit → exponential backoff + jitter | `recovery.py` `_handle_rate_limit` | any *semantic* failure |
| Custom string-matched handlers | `recovery.py` `register_handler` | anything without a handler |

`ToolValidationError` and "not found" errors are returned raw to the LLM
(`recovery.py`), which then improvises a fix on the next turn — unbounded,
inconsistent, and expensive (a full context re-processing). Meanwhile the
workflow engine already has the right idea (`IterativeRefineNode`, bounded
refine with `refine_feedback`, `backend/StAgent/workflow/nodes/builtin/iterative_refine.py`)
but the agent loop cannot reuse it.

### 2.2 Design

**A failure reflector** (`StAgent/agent/reflection.py`) classifies a failed
tool call and proposes one bounded corrective action. It runs **only on the
error path**, so successful tool calls pay zero extra cost.

```
tool_call fails
    │
    ▼
ErrorRecovery middleware (existing: timeout / rate-limit / custom) ──► handled ──► retry
    │ not handled
    ▼
Reflector.classify(tool_call, error, step_context[truncated])
    │
    ▼
ReflectionResult{kind, plan, retry_params?, context_request?, message}
```

| `kind` | Example | Corrective action |
|---|---|---|
| `retryable_params` | wrong/missing params | rewrite params (≤1 retry) |
| `context_gap` | needs a file/doc not in context | pull one context source (attachment / gated policy / doc chunk) → retry (≤1) |
| `transient` | timeout / 429 | hand back to existing recovery paths |
| `env_blocked` | permission / missing dependency | no retry; emit a precise report to the user/upstream |
| `unknown` | — | return raw to the LLM (today's behavior) |

**Contract**

```python
@dataclass
class ReflectionResult:
    kind: Literal["retryable_params", "context_gap", "transient", "env_blocked", "unknown"]
    retry_params: dict[str, Any] | None = None
    context_request: str | None = None   # what to pull before retrying
    message: str = ""                    # for env_blocked / unknown
```

**Boundedness**

- `max_reflections_per_turn = 2` (default; configurable).
- At most **one** corrected retry per tool call per turn.
- Reflection runs on the fast tier (`tier2` alias when available), temperature
  0, output capped (~512 tokens).
- Failure of the reflector itself → treat as `unknown` (never worsens the
  current behavior).

**Integration point**

The single slot is `_dispatch_tools` in `backend/StAgent/agent/query.py`
(`base = await recovery.as_middleware()(tool_call, ctx)` at the middleware
chain): insert the reflector *after* the existing `ErrorRecovery` middleware
and before the raw-error fallback. No other call sites change; sub-agents and
Mission segments inherit it because they share the same dispatcher.

**Persistence / observability (P0)**

- No new table in P0 — reflection records are emitted as span events on the
  existing `agent_trace_spans` (`TraceStore`).
- Event `agent_reflection{kind, tool, action}` + Prometheus histogram
  `agent_reflection_outcomes` (feeds the Module B distiller).
- Optional P1 table `agent_reflections` if aggregate analytics outgrow spans.

### 2.3 Testing

- Unit: taxonomy dispatch (one case per `kind`); cap enforcement
  (`max_reflections_per_turn`, single retry per tool).
- Integration (mock LLM): `retryable_params` → corrected retry succeeds;
  `context_gap` → context pulled, retry succeeds; `env_blocked` → user-facing
  report, no retry; `unknown` → byte-identical to today's fallback.
- Regression: reflector disabled == current behavior (flag `reflection_enabled`).

### 2.4 Phases

| Phase | Scope |
|---|---|
| P0 | reflector + `retryable_params` + `transient` + caps + span events |
| P1 | `context_gap` (context-source pull) + `env_blocked` reports + metrics dashboard |
| P2 | adaptive model choice (tier1 on second failure), per-tool reflect policies |

---

## 3. Module B — Lesson Memory (失败教训蒸馏)

### 3.1 Motivation & evidence

Procedural memory only ever records **success**:

- `backend/StAgent/memory/formation.py` — procedural formation requires
  `tool_success_count > 0 and tool_failure_count == 0` (≈ lines 134–139);
  a failed turn triggers only `TOOL_FAILURE` (weight 0.05), which never
  crosses the `PROCEDURAL 0.35` threshold.
- Background agent tasks disable memory outright
  (`backend/StAgent/tasks/handlers/agent_handler.py` — `MemoryPolicy(enabled=False)`).

Result: the agent can recall "how I solved X" but never "how I failed at X and
what unblocked me" — the most expensive lesson to re-learn is the one thrown
away.

### 3.2 Design

**Formation** — add a `LESSON` trigger to `FormationPolicy`:

| Trigger | Weight | Fires when |
|---|---|---|
| `LESSON` (new) | 0.45 | turn ended with failure **and** a recovery is recorded (Module A reflection, a corrected retry, or the user's "记住教训" directive) |

**Distillation** — a `LessonDistiller` runs **off the hot path** after
`AgentMemory.observe_turn` (same post-turn site `AgentController._record_episode`
/ `TaskHistoryHook` already uses; idempotency by `(session_id, turn_id)` is
already enforced). Inputs: turn summary + outcome + reflection records
(Module A) + the final user feedback. Output:

```python
@dataclass
class Lesson:
    title: str          # "用 web_fetch 访问本地文件路径会失败"
    anti_pattern: str   # 失败模式描述
    worked_around: str  # 最终解法 / 上报方式
    tags: list[str]     # 工具名 / 领域标签
    confidence: float   # 0..1
```

**Storage** — procedural memory rows gain `kind = "lesson"` and an
`anti_pattern` tag (no schema change: the procedural store already persists
structured entries; tags ride in existing metadata). Dedup by embedding
similarity against existing procedures (reuse the recall pipeline's vector +
lexical + BM25 path); a near-duplicate bumps `confidence` instead of creating
a row.

**Recall injection** — `RecallSource` (`backend/StAgent/context/sources/recall.py`)
already injects recall as `<attachment kind="recall">` under a 2 s budget.
Lessons are injected as `<attachment kind="lesson">` with a priority boost
when query similarity exceeds a threshold; bounded (≤3 lessons per turn,
within the same recall budget).

**Hygiene** — confidence decay per non-hit recall; cap N lessons per tag;
user-dismissible (`remember`/`forget` extension); global kill switch
(`lesson_memory_enabled`). Mission mode and background tasks opt **in** to
lesson formation (fixing `agent_handler.py`'s blanket disable for this path
only, behind the same switch).

### 3.3 Testing

- Unit: formation weights (failure without recovery → no lesson; failure with
  reflection → lesson); distiller output contract; dedup bumps confidence.
- Integration (mock LLM): post-turn distillation runs async and does not block
  the turn; recall injection order (recall before lesson, cap respected).
- Regression: distiller disabled → no formation change from today.

### 3.4 Phases

| Phase | Scope |
|---|---|
| P0 | `LESSON` trigger + distiller + procedural write + recall injection |
| P1 | anti-pattern tags + confidence decay + dismiss (`forget`) |
| P2 | lessons surfaced in the Memory page UI + weekly "lessons digest" |

---

## 4. Module C — Parallel Crew (并行子代理工作池)

### 4.1 Motivation & evidence

Sub-agent delegation is serial and unbounded:

- `AgentTool.is_concurrency_safe = False` (`backend/StAgent/agent/subagent.py`)
  — a turn can never dispatch sub-agents concurrently with other tools.
- No recursion depth limit anywhere — `AgentTool` is not even in its own
  denied list (`DEFAULT_AGENT_TOOL_DENIED_TOOLS` excludes only heavy tools like
  `coding_agent`), so unbounded nesting is possible.
- Results are a flat dict (`text/success/steps_count/...`); only the
  `coding_agent` variant merges `file_state` back. No shared artifact channel.
- The executor already has the concurrency machinery (`max_parallel=10` in
  `make_child_executor`) — it is just not exposed to sub-agent fan-out.

### 4.2 Design

**New tool `crew_fork`** (category `util`), which fans one turn out to N
sub-agents and merges the results:

```
crew_fork(tasks=[{name, prompt, agent?, max_turns}], merge_policy="concat")
    │
    ├─ depth guard: crew_depth ≥ max_crew_depth (default 1) → refuse
    ├─ parallel: asyncio.gather over child engines
    │     · per-child budget: max_turns (1–30, default 8), tool timeout
    │     · bound: len(tasks) ≤ max_parallel (10)
    │     · abort cascade via the existing abort bridge
    ├─ shared artifacts: children return FileRef lists (file layer);
    │     the crew manifest is exposed via ToolResult.produced_files so the
    │     existing ArtifactRegistrar picks everything up
    └─ merge: deterministic concat of summaries + manifest union
          (+ optional merge-LLM in P1 for conflict arbitration)
```

**Key decisions**

| Decision | Choice |
|---|---|
| Concurrency | `crew_fork` marks `is_concurrency_safe = True` so the kernel may batch it with other safe tools; internal fan-out via `asyncio.gather` |
| Depth guard | `crew_depth` counter carried in `ToolContext`; refuse at `max_crew_depth` (default 1, i.e. crew members may use tools but not spawn crews) |
| Denied tools | `crew_fork` joins `DEFAULT_AGENT_TOOL_DENIED_TOOLS` for children — no recursive crews |
| Artifacts | File layer `FileRef` manifest (never base64) — same pattern as the art nodes' `MediaRef` |
| Budgets | children respect the runtime profile (`max_turns`, tool timeout); total fan-out bounded by `max_parallel`; per-user concurrency limit unchanged |
| Existing `AgentTool` | untouched — `crew_fork` is additive; serial delegation remains available |

### 4.3 Testing

- Unit: depth-cap rejection; fan-out bounded (10); merge determinism.
- Integration (mock LLM): N independent children → order-independent merged
  result; artifact manifest registration; abort cascade kills all children.
- Regression: `AgentTool` behavior byte-identical; `is_concurrency_safe`
  partition rules unchanged for other tools.

### 4.4 Phases

| Phase | Scope |
|---|---|
| P0 | `crew_fork` + gather fan-out + depth guard + manifest + concat merge |
| P1 | merge-LLM conflict arbitration + artifact dedup + per-child agent profile selection |
| P2 | ReactFlow crew preview in chat (reuse the workflow graph preview), crew templates |

---

## 5. Cross-cutting concerns

| Concern | Policy |
|---|---|
| Kernel contract | All three modules sit at existing seams (recovery middleware, memory formation, tool layer). `run_loop` / `QueryEngine.submit_message` untouched; canonical agent path preserved (`docs/technical/execution-topology.md`) |
| Budgets | Reflection: `max_reflections_per_turn`, tier2, 512-token cap. Distillation: off hot path, per-turn cap. Crew: depth ≤1, fan-out ≤10, child `max_turns` |
| Security | `human_gate` rules unchanged and never bypassed; crew children inherit tool whitelists; depth guard prevents fork bombs |
| Observability | `agent_reflection{...}` events + histogram; lesson formation counters; crew runs register `ExecutionRun` children (`parent_run_id`) so OTel spans chain; all visible in `TraceStore` |
| Memory hygiene | Lessons deduped, decayed, dismissible, kill-switchable — no unbounded procedural growth |

## 6. Sequencing with Mission Mode

| Layer | Owned by | Interacts with |
|---|---|---|
| Plan / step / verification / continuation | [Mission Mode](mission-mode.md) | crew steps can call `crew_fork`; mission segments inherit reflection (shared dispatcher) |
| Tool-level fix | Module A | feeds failure evidence to Module B |
| Cross-session experience | Module B | mission turns also distill lessons (opt-in) |
| Turn-level parallelism | Module C | complements Mission P2's task-queue parallelism |

Recommended order: **A → B → C** (each builds on the previous one's outputs).
The standalone "Auto-pilot" idea (auto-resume of budget-exhausted chat turns)
is a quick win that reuses the Mission continuer and is intentionally out of
scope here.

## 7. Rollout plan

| Phase | Deliverables | Milestone |
|---|---|---|
| P0 (A) | Reflector + taxonomy + caps + span events | a deliberately-broken tool call self-corrects within one turn, bounded |
| P1 (B) | LESSON trigger + distiller + recall injection | a repeated failure gets auto-fixed from a recalled lesson in a later session |
| P2 (C) | `crew_fork` + fan-out + depth guard + manifest | one turn decomposes into 4 parallel sub-agents and merges artifacts |
| P3 | merge-LLM, lesson UI, adaptive reflection model | polish + observability dashboards |

Each phase lands independently with its own tests and can be skipped without
invalidating the others.

## 8. Risks & mitigations

| Risk | Mitigation |
|---|---|
| Reflection latency/cost on the error path | tier2 fast model, temperature 0, 512-token cap, 2/turn cap |
| Wrong reflection makes things worse | single corrected retry per tool; `unknown` fallback = today's behavior; feature flag |
| Lesson pollution / hallucinations | formation threshold, similarity dedup, confidence decay, user dismiss, kill switch |
| Fork bomb / concurrency storm | depth cap + fan-out cap + per-user concurrency limit from the runtime profile |
| Context bloat from lessons | ≤3 lessons per turn within the existing 2 s recall budget |
| Crew result quality varies | merge policy is deterministic in P0; optional merge-LLM gated in P1 |

## 9. Open questions

1. Should the reflector always use `tier2`, or escalate to `tier1` when the
   first corrected retry also fails?
2. Are lessons global or per-user? (Recommendation: per-user with
   `public` lessons in P2.)
3. Should `crew_fork` be callable inside Mission steps by default, or gated by
   mission autonomy level?

## References

- [`mission-mode.md`](mission-mode.md) — the planning/verification/continuation layer this program completes
- [`execution-topology.md`](execution-topology.md) — kernel contract and run correlation
- [`agent-trace.md`](agent-trace.md) — `TraceStore` / span model used for reflection records
- `backend/StAgent/agent/recovery.py` — `ErrorRecovery` middleware (extension point)
- `backend/StAgent/agent/query.py` — `_dispatch_tools` (insertion point)
- `backend/StAgent/memory/formation.py` — `FormationPolicy` (new trigger)
- `backend/StAgent/context/sources/recall.py` — `RecallSource` (lesson injection)
- `backend/StAgent/agent/subagent.py` — `AgentTool` / child executor (fan-out base)
- `backend/StAgent/workflow/nodes/builtin/iterative_refine.py` — bounded-refine precedent
