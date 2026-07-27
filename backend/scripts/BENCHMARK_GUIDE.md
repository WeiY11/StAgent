# LeAgent Benchmark Guide

This guide documents the lightweight chat streaming benchmark in
`scripts/perf_benchmark.py`.

## Prerequisites

- Start the backend locally so `http://localhost:7860/api/v1` is reachable.
- Run commands from the backend project with `uv`:

```bash
cd backend
uv run python ../scripts/perf_benchmark.py --requests 5 --concurrency 3
```

The harness uses only Python stdlib modules, but `uv run` keeps execution
consistent with the backend environment.

## Common Runs

Smoke test:

```bash
cd backend
uv run python ../scripts/perf_benchmark.py \
  --requests 1 \
  --concurrency 1 \
  --timeout 30 \
  --message "Perf smoke request {i}: respond with OK."
```

Small baseline:

```bash
cd backend
uv run python ../scripts/perf_benchmark.py \
  --requests 5 \
  --concurrency 3 \
  --timeout 120 \
  --new-session-per-request \
  --message "Benchmark turn {i}: answer 2+2 in one short sentence." \
  --output ../scripts/perf_latest.json
```

Slow-consumer backpressure test:

```bash
cd backend
uv run python ../scripts/perf_benchmark.py \
  --requests 2 \
  --concurrency 2 \
  --timeout 120 \
  --slow-consumer-ms 50 \
  --new-session-per-request \
  --message "Slow consumer test {i}: say hello." \
  --output ../scripts/perf_slow_consumer.json
```

Custom backend URL:

```bash
cd backend
uv run python ../scripts/perf_benchmark.py \
  --base-url http://localhost:7860/api/v1 \
  --requests 10 \
  --concurrency 5
```

Concurrency pressure tiers (runs each tier sequentially, one report each):

```bash
cd backend
uv run python ../scripts/perf_benchmark.py \
  --requests 24 \
  --concurrency-tiers 1,4,8 \
  --new-session-per-request \
  --message "Pressure tier turn {i}: answer 2+2 briefly." \
  --output ../scripts/perf_tiers.json
```

Multi-turn session reuse (each worker creates one session, then sends N turns):

```bash
cd backend
uv run python ../scripts/perf_benchmark.py \
  --requests 5 \
  --concurrency 3 \
  --turns-per-session 4 \
  --message "Reuse worker {i} turn {turn}: continue the thread." \
  --output ../scripts/perf_reuse.json
```

## Parameters

- `--requests`: number of workers (default 20). With `--turns-per-session N`
  the total streamed turns is `requests * N`.
- `--concurrency`: in-flight worker cap (default 5). Ignored when
  `--concurrency-tiers` is set.
- `--concurrency-tiers`: comma-separated levels (e.g. `1,4,8`) run one after
  another; the report becomes a top-level `tiers` array instead of a single
  `summary`/`samples` pair.
- `--turns-per-session`: turns per session; `>1` switches the scenario to
  `chat_stream_reuse` and creates one session per worker via
  `POST /chat/sessions`. The `{turn}` placeholder is available in `--message`.
- `--slow-consumer-ms`, `--session-id`, `--new-session-per-request`,
  `--timeout`, `--message`, `--output`: unchanged from before.
- `--token`: bearer token sent as `Authorization` on every request (also read
  from `LEAGENT_PERF_TOKEN`). Required when the backend enforces auth.
- `--warmup N`: fire `N` throwaway streamed requests before timing so
  cold-start cost never lands in the measured window. Recorded as
  `environment.warmup`.
- `--require-terminal-event` / `--no-require-terminal-event`: on by default, a
  2xx stream is a success **only** if it ends with `data: [DONE]`; otherwise the
  sample is a failure with `error="missing_terminal_event"`. Opt out only when
  deliberately measuring a non-terminating endpoint.
- `--provider`, `--model`: recorded in `environment` (also read from
  `LEAGENT_PERF_PROVIDER` / `LEAGENT_PERF_MODEL`) so a report cannot be
  mistaken for a different configuration.
- `--min-success-rate` (default `0.99`) and `--baseline-output PATH`: see
  *Trusted baselines* below.

## Trusted baselines vs illustrative samples

A small ad-hoc run is an *illustrative sample*, not a baseline. `--baseline-output`
writes a file **only** when the run clears every guard:

- at least **100** requests,
- every stream terminated with `data: [DONE]`, and
- success rate `>= --min-success-rate`.

Otherwise the harness prints the rejection reason and exits `2` without writing
anything (an existing baseline is never clobbered — the write is atomic via a
temp file + `os.replace`). A produced baseline carries
`"evidence_kind": "measured_baseline"`; any other run is `"run"`. The committed
`scripts/perf_baseline.json` is only `"evidence_kind": "illustrative_sample"`.

```bash
cd backend
uv run python scripts/perf_benchmark.py \
  --requests 120 --concurrency 8 --new-session-per-request \
  --token "$LEAGENT_PERF_TOKEN" --warmup 5 \
  --provider deepseek --model deepseek-chat \
  --min-success-rate 0.99 \
  --baseline-output scripts/perf_baseline.measured.json
```

## Output

The benchmark prints JSON and optionally writes it with `--output`.

Important fields:

- `summary.success_rate`: request success ratio.
- `summary.first_event_ms`: time to first non-empty SSE line. This is the
  closest harness-level proxy for chat perceived responsiveness.
- `summary.total_ms`: total streaming duration.
- `summary.throughput_rps`: successful turns per second over the batch
  wall-clock; the primary saturation signal across concurrency tiers.
- `summary.error_classification`: counts bucketed as `ok`, `http_4xx`,
  `http_5xx`, `missing_terminal_event`, `timeout`, `connection`,
  `session_create_failed`, `other`.
- `environment`: provenance for the run — `git_sha`, `provider`, `model`,
  `python`, `platform`, `warmup`.
- `evidence_kind`: `measured_baseline` for a validated `--baseline-output` run,
  otherwise `run`.
- `elapsed_ms`: wall-clock time for the full benchmark batch (sum across tiers
  in multi-tier mode).
- `tiers[]`: present only for `--concurrency-tiers`; each entry has
  `concurrency`, `elapsed_ms`, `summary`, and `samples`.
- `samples[].bytes_read`: approximate stream payload size.
- `samples[].terminal_event_seen`: whether the stream ended with `data: [DONE]`.
- `samples[].error`: transport, HTTP, or `missing_terminal_event` details.

Example result from a local run:

```json
{
  "requests": 5,
  "success": 5,
  "error": 0,
  "success_rate": 1.0,
  "first_event_ms": {
    "p50": 156.9,
    "p95": 182.7,
    "p99": 182.7
  },
  "total_ms": {
    "p50": 3813.0,
    "p95": 4860.4,
    "p99": 4860.4
  }
}
```

## Interpreting Results

- Track `first_event_ms` for user-perceived chat latency regressions.
- Track `total_ms` for provider generation speed and end-to-end streaming
  overhead.
- Use `--new-session-per-request` to stress session creation and first-turn
  setup. The harness omits `session_id` in this mode so the API creates an
  owned session with the authenticated local user.
- Use `--session-id` only with an existing chat session UUID. Sending an
  arbitrary UUID can make the stream response succeed while later persistence
  fails foreign-key checks.
- Use `--slow-consumer-ms` to validate stream queue backpressure behavior.
- Use `--concurrency-tiers` to find the saturation knee: watch where
  `throughput_rps` stops rising while `first_event_ms`/`total_ms` climb.
- Use `--turns-per-session` to exercise session-reuse hot paths (context
  reload, history growth) rather than only first-turn setup.
- Watch `error_classification` for `http_5xx`/`timeout` spikes under load; a
  rising share signals the backend is past its stable concurrency.

For deeper diagnosis, compare benchmark output with Prometheus metrics such as
`leagent_llm_stream_ttfb_seconds`,
`leagent_agent_turn_phase_duration_seconds`,
`leagent_db_query_duration_seconds`, and
`leagent_agent_stream_queue_depth_observed`.

## Notes

- Results depend heavily on the configured LLM provider, model, network, and
  prompt length.
- Keep baseline JSON files in `scripts/` only when they are useful for
  comparing a specific optimization.
- Prefer short, deterministic prompts when measuring framework overhead.
