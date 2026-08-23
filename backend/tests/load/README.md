# StAgent load-test harness

Both harnesses drive the **real** HTTP surface. They target the multipart chat
SSE stream (`POST /api/v1/chat/stream`) and treat a 2xx response that never
emits `data: [DONE]` as a failure — the same terminal-event truth enforced by
`scripts/perf_benchmark.py`. There is no `/api/v1/chat`, `/api/v1/flows/execute`,
`/api/v1/tools/invoke`, or `/api/v1/llm/complete` route, so no profile calls one.

| Tool   | Scope                                                              | Entrypoint       |
| ------ | ----------------------------------------------------------------- | ---------------- |
| Locust | HTTP: `/api/v1/chat/stream`, `/api/v1/workflow/flows/{id}/run` (when `StAgent_FLOW_ID` is set), `/api/v1/files/upload`, `/health` | `locustfile.py`  |
| k6     | HTTP: `/api/v1/chat/stream` sustained arrival rate                | `k6_agent.js`    |

## Running locally

```bash
# HTTP profile (web-like mix)
pip install locust
StAgent_TOKEN=<token> \
locust -f tests/load/locustfile.py --host=http://localhost:7860 \
  --users=200 --spawn-rate=20 --run-time=2m --headless \
  --csv=reports/locust

# k6 streaming profile
StAgent_BASE_URL=http://localhost:7860 StAgent_TOKEN=<token> \
k6 run tests/load/k6_agent.js --summary-export=reports/k6_agent.json
```

## CI performance gates

`ci-perf.sh` runs the 50 RPS / 2 min smoke and asserts both gates:

* p95 HTTP latency ≤ `LEAGENT_PERF_P95_BUDGET_MS` (default **750 ms**)
* error rate ≤ `LEAGENT_PERF_ERROR_BUDGET` (default **1%**)

Both budgets are read from the environment, so a slower target can raise them
explicitly rather than by editing code.

There is no committed baseline. A trusted baseline must be measured on the
target host with `scripts/perf_benchmark.py --baseline-output ...` (≥100
requests, every stream terminated, success rate above the threshold); the result
carries `"evidence_kind": "measured_baseline"`. `scripts/perf_baseline.json` is
an illustrative sample only (`"evidence_kind": "illustrative_sample"`).
