// k6 scenario: sustained load on the real chat streaming endpoint.
// Posts multipart form data to /api/v1/chat/stream (matching the app + the
// perf_benchmark harness) and treats a 2xx stream without a `data: [DONE]`
// terminator as a failure.

import http from 'k6/http';
import { check, sleep } from 'k6';
import { Trend, Rate } from 'k6/metrics';

const p95 = new Trend('p95_latency');
const failures = new Rate('failures');

export const options = {
  scenarios: {
    steady: {
      executor: 'constant-arrival-rate',
      rate: 50,
      timeUnit: '1s',
      duration: '2m',
      preAllocatedVUs: 50,
      maxVUs: 200,
    },
  },
  thresholds: {
    p95_latency: ['p(95)<750'],
    failures: ['rate<0.01'],
  },
};

const BASE_URL = __ENV.StAgent_BASE_URL || 'http://localhost:7860';
const TOKEN = __ENV.StAgent_TOKEN || '';

export default function () {
  // Empty session_id lets the API create an owned session for the caller.
  const headers = {
    Accept: 'text/event-stream',
    ...(TOKEN ? { Authorization: `Bearer ${TOKEN}` } : {}),
  };
  const res = http.post(
    `${BASE_URL}/api/v1/chat/stream`,
    { message: 'hello k6', session_id: '' },
    { headers }
  );
  const ok2xx = res.status >= 200 && res.status < 300;
  const terminated = typeof res.body === 'string' && res.body.includes('data: [DONE]');
  p95.add(res.timings.duration);
  failures.add(!(ok2xx && terminated));
  check(res, {
    'status 2xx': () => ok2xx,
    'stream terminated': () => terminated,
  });
  sleep(0.1);
}
