#!/usr/bin/env python
"""Lightweight StAgent performance benchmark harness.

Targets a running backend and measures chat streaming latency under load:
request upload, time to first SSE event, total stream time, success rate,
throughput (req/s), and error classification. It intentionally uses only
stdlib modules so it can run from any checkout.

Scenarios:
- chat_stream: independent streaming requests (default).
- chat_stream_reuse: multi-turn reuse where each worker creates one session
  and sends several sequential turns to it (``--turns-per-session``).

Load profiles:
- Single concurrency (``--concurrency``) produces a flat report.
- Multiple concurrency tiers (``--concurrency-tiers 1,4,8``) run sequentially
  and produce a per-tier report for pressure comparisons.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import statistics
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from typing import Any
from uuid import uuid4


@dataclass
class Sample:
    scenario: str
    ok: bool
    status: int
    first_event_ms: float
    total_ms: float
    bytes_read: int
    error: str = ""
    terminal_event_seen: bool = False


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((pct / 100) * (len(ordered) - 1)))))
    return ordered[index]


def _classify_error(sample: Sample) -> str:
    """Bucket a sample into a coarse outcome category for aggregation."""
    if sample.ok:
        return "ok"
    if sample.status and 400 <= sample.status < 500:
        return "http_4xx"
    if sample.status and 500 <= sample.status < 600:
        return "http_5xx"
    lowered = sample.error.lower()
    if "missing_terminal_event" in lowered:
        return "missing_terminal_event"
    if "timed out" in lowered or "timeout" in lowered:
        return "timeout"
    if "refused" in lowered or "connection" in lowered or "urlopen" in lowered:
        return "connection"
    if "session_create_failed" in lowered:
        return "session_create_failed"
    return "other"


def _is_terminal_line(line: bytes) -> bool:
    """True when an SSE line is the canonical ``data: [DONE]`` terminator."""
    stripped = line.strip()
    if not stripped.startswith(b"data:"):
        return False
    return stripped[len(b"data:") :].strip() == b"[DONE]"


def _sample_from_stream(
    *,
    status: int,
    lines: list[bytes],
    scenario: str = "chat_stream",
    first_event_ms: float = 0.0,
    total_ms: float = 0.0,
    bytes_read: int = 0,
    require_terminal_event: bool = True,
) -> Sample:
    """Build a Sample from a completed SSE body, enforcing terminal-event truth.

    A 2xx response is only a success when the stream actually terminated with
    ``data: [DONE]`` (unless the caller opts out via ``require_terminal_event``).
    """
    terminal_event_seen = any(_is_terminal_line(line) for line in lines)
    http_ok = 200 <= status < 300
    ok = http_ok and (terminal_event_seen or not require_terminal_event)
    error = ""
    if http_ok and require_terminal_event and not terminal_event_seen:
        error = "missing_terminal_event"
    return Sample(
        scenario=scenario,
        ok=ok,
        status=status,
        first_event_ms=first_event_ms,
        total_ms=total_ms,
        bytes_read=bytes_read or sum(len(line) for line in lines),
        error=error,
        terminal_event_seen=terminal_event_seen,
    )


def _baseline_is_valid(
    samples: list[Sample],
    *,
    min_requests: int,
    min_success_rate: float,
) -> tuple[bool, str]:
    """Reject a baseline that is too small or too unreliable to trust."""
    if len(samples) < min_requests:
        return False, f"too few requests: {len(samples)} < {min_requests}"
    ok = sum(1 for s in samples if s.ok)
    rate = ok / len(samples) if samples else 0.0
    if rate < min_success_rate:
        return False, f"success rate {rate:.3f} < {min_success_rate:.3f}"
    return True, ""


def _summarize(samples: list[Sample], *, elapsed_ms: float | None = None) -> dict[str, Any]:
    ok = [s for s in samples if s.ok]
    first = [s.first_event_ms for s in ok]
    total = [s.total_ms for s in ok]

    classification: dict[str, int] = {}
    for sample in samples:
        key = _classify_error(sample)
        classification[key] = classification.get(key, 0) + 1

    summary: dict[str, Any] = {
        "requests": len(samples),
        "success": len(ok),
        "error": len(samples) - len(ok),
        "success_rate": len(ok) / len(samples) if samples else 0.0,
        "first_event_ms": {
            "p50": statistics.median(first) if first else 0.0,
            "p95": _percentile(first, 95),
            "p99": _percentile(first, 99),
        },
        "total_ms": {
            "p50": statistics.median(total) if total else 0.0,
            "p95": _percentile(total, 95),
            "p99": _percentile(total, 99),
        },
        "error_classification": classification,
    }
    if elapsed_ms and elapsed_ms > 0:
        # Throughput counts completed (successful) turns over wall-clock time.
        summary["throughput_rps"] = len(ok) / (elapsed_ms / 1000)
    return summary


def _encode_multipart(fields: dict[str, str]) -> tuple[bytes, str]:
    boundary = f"----StAgent-perf-{uuid4().hex}"
    body = bytearray()
    for name, value in fields.items():
        if value == "":
            continue
        body.extend(f"--{boundary}\r\n".encode())
        body.extend(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        body.extend(value.encode())
        body.extend(b"\r\n")
    body.extend(f"--{boundary}--\r\n".encode())
    return bytes(body), f"multipart/form-data; boundary={boundary}"


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"} if token else {}


def _create_session(base_url: str, timeout: float, token: str = "") -> str:
    """Create an owned chat session and return its id for multi-turn reuse."""
    url = urllib.parse.urljoin(base_url.rstrip("/") + "/", "chat/sessions")
    data = json.dumps({}).encode()
    request = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            **_auth_headers(token),
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return str(payload.get("id", ""))


def _read_stream(
    *,
    url: str,
    message: str,
    session_id: str,
    timeout: float,
    slow_consumer_ms: int,
    scenario: str = "chat_stream",
    token: str = "",
    require_terminal_event: bool = True,
) -> Sample:
    body, content_type = _encode_multipart({"message": message, "session_id": session_id})
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": content_type,
            "Accept": "text/event-stream",
            **_auth_headers(token),
        },
    )
    started = time.perf_counter()
    first_event_at: float | None = None
    lines: list[bytes] = []
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = int(response.status)
            while True:
                line = response.readline()
                if not line:
                    break
                lines.append(line)
                if first_event_at is None and line.strip():
                    first_event_at = time.perf_counter()
                if slow_consumer_ms > 0:
                    time.sleep(slow_consumer_ms / 1000)
        total = time.perf_counter() - started
        return _sample_from_stream(
            status=status,
            lines=lines,
            scenario=scenario,
            first_event_ms=((first_event_at or time.perf_counter()) - started) * 1000,
            total_ms=total * 1000,
            require_terminal_event=require_terminal_event,
        )
    except urllib.error.HTTPError as exc:
        total = time.perf_counter() - started
        return Sample(
            scenario=scenario,
            ok=False,
            status=exc.code,
            first_event_ms=0.0,
            total_ms=total * 1000,
            bytes_read=sum(len(line) for line in lines),
            error=str(exc),
        )
    except Exception as exc:  # noqa: BLE001
        total = time.perf_counter() - started
        return Sample(
            scenario=scenario,
            ok=False,
            status=0,
            first_event_ms=0.0,
            total_ms=total * 1000,
            bytes_read=sum(len(line) for line in lines),
            error=str(exc),
        )


async def _run_batch(args: argparse.Namespace, concurrency: int) -> tuple[list[Sample], float]:
    """Run one load batch at a fixed concurrency and return samples + elapsed_ms."""
    stream_url = urllib.parse.urljoin(args.base_url.rstrip("/") + "/", "chat/stream")
    semaphore = asyncio.Semaphore(max(1, concurrency))
    samples: list[Sample] = []
    turns = max(1, args.turns_per_session)

    async def reuse_worker(index: int) -> None:
        async with semaphore:
            try:
                session_id = await asyncio.to_thread(
                    _create_session, args.base_url, args.timeout, args.token
                )
            except Exception as exc:  # noqa: BLE001
                samples.append(
                    Sample(
                        scenario="chat_stream_reuse",
                        ok=False,
                        status=0,
                        first_event_ms=0.0,
                        total_ms=0.0,
                        bytes_read=0,
                        error=f"session_create_failed: {exc}",
                    )
                )
                return
            for turn in range(turns):
                sample = await asyncio.to_thread(
                    _read_stream,
                    url=stream_url,
                    message=args.message.format(i=index, turn=turn),
                    session_id=session_id,
                    timeout=args.timeout,
                    slow_consumer_ms=args.slow_consumer_ms,
                    scenario="chat_stream_reuse",
                    token=args.token,
                    require_terminal_event=args.require_terminal_event,
                )
                samples.append(sample)

    async def single_worker(index: int) -> None:
        async with semaphore:
            # Empty session_id lets the API create an owned ChatSession with the
            # authenticated local user. Sending an arbitrary UUID makes the
            # stream succeed but later DB persistence can fail FK/NOT NULL checks.
            session_id = "" if args.new_session_per_request else args.session_id
            sample = await asyncio.to_thread(
                _read_stream,
                url=stream_url,
                message=args.message.format(i=index, turn=0),
                session_id=session_id,
                timeout=args.timeout,
                slow_consumer_ms=args.slow_consumer_ms,
                token=args.token,
                require_terminal_event=args.require_terminal_event,
            )
            samples.append(sample)

    worker = reuse_worker if turns > 1 else single_worker
    started = time.perf_counter()
    await asyncio.gather(*(worker(i) for i in range(args.requests)))
    elapsed_ms = (time.perf_counter() - started) * 1000
    return samples, elapsed_ms


def _config(args: argparse.Namespace, tiers: list[int]) -> dict[str, Any]:
    return {
        "base_url": args.base_url,
        "requests": args.requests,
        "concurrency": args.concurrency,
        "concurrency_tiers": tiers,
        "turns_per_session": max(1, args.turns_per_session),
        "slow_consumer_ms": args.slow_consumer_ms,
        "new_session_per_request": args.new_session_per_request,
        "require_terminal_event": args.require_terminal_event,
        "authenticated": bool(args.token),
    }


def _git_sha() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:  # noqa: BLE001
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def _environment(args: argparse.Namespace) -> dict[str, Any]:
    """Provenance so a report cannot be mistaken for a different build/host."""
    return {
        "git_sha": _git_sha(),
        "provider": args.provider or os.environ.get("StAgent_PERF_PROVIDER", ""),
        "model": args.model or os.environ.get("StAgent_PERF_MODEL", ""),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "warmup": args.warmup,
    }


async def _warmup(args: argparse.Namespace) -> None:
    """Fire throwaway requests so cold-start cost never pollutes the baseline."""
    if args.warmup <= 0:
        return
    stream_url = urllib.parse.urljoin(args.base_url.rstrip("/") + "/", "chat/stream")
    for i in range(args.warmup):
        await asyncio.to_thread(
            _read_stream,
            url=stream_url,
            message=args.message.format(i=i, turn=0),
            session_id="",
            timeout=args.timeout,
            slow_consumer_ms=0,
            token=args.token,
            require_terminal_event=args.require_terminal_event,
        )


def _parse_tiers(raw: str, fallback: int) -> list[int]:
    if not raw.strip():
        return [fallback]
    tiers = [int(part) for part in raw.split(",") if part.strip()]
    return tiers or [fallback]


async def _run(args: argparse.Namespace) -> tuple[dict[str, Any], list[Sample]]:
    tiers = _parse_tiers(args.concurrency_tiers, args.concurrency)
    await _warmup(args)
    environment = _environment(args)

    if len(tiers) == 1:
        samples, elapsed_ms = await _run_batch(args, tiers[0])
        report = {
            "config": _config(args, tiers),
            "environment": environment,
            "elapsed_ms": elapsed_ms,
            "summary": _summarize(samples, elapsed_ms=elapsed_ms),
            "samples": [asdict(s) for s in samples],
        }
        return report, samples

    tier_reports: list[dict[str, Any]] = []
    all_samples: list[Sample] = []
    total_elapsed = 0.0
    for tier in tiers:
        samples, elapsed_ms = await _run_batch(args, tier)
        total_elapsed += elapsed_ms
        all_samples.extend(samples)
        tier_reports.append(
            {
                "concurrency": tier,
                "elapsed_ms": elapsed_ms,
                "summary": _summarize(samples, elapsed_ms=elapsed_ms),
                "samples": [asdict(s) for s in samples],
            }
        )
    report = {
        "config": _config(args, tiers),
        "environment": environment,
        "elapsed_ms": total_elapsed,
        "tiers": tier_reports,
    }
    return report, all_samples


def main() -> None:
    parser = argparse.ArgumentParser(description="Run StAgent chat stream benchmarks.")
    parser.add_argument("--base-url", default="http://localhost:7860/api/v1")
    parser.add_argument("--requests", type=int, default=20)
    parser.add_argument("--concurrency", type=int, default=5)
    parser.add_argument(
        "--concurrency-tiers",
        default="",
        help=(
            "Comma-separated concurrency levels to run sequentially, e.g. "
            "'1,4,8'. Overrides --concurrency and produces a per-tier report."
        ),
    )
    parser.add_argument(
        "--turns-per-session",
        type=int,
        default=1,
        help=(
            "Turns sent per session. When >1 each worker creates one session "
            "and reuses it across sequential turns (chat_stream_reuse)."
        ),
    )
    parser.add_argument("--timeout", type=float, default=900.0)
    parser.add_argument("--slow-consumer-ms", type=int, default=0)
    parser.add_argument(
        "--session-id",
        default="",
        help=(
            "Existing chat session UUID. Omit to let the API create a valid "
            "owned session for each request."
        ),
    )
    parser.add_argument("--new-session-per-request", action="store_true")
    parser.add_argument(
        "--message",
        default="Perf smoke request {i}: answer with one short sentence.",
    )
    parser.add_argument("--output", default="")
    parser.add_argument(
        "--token",
        default=os.environ.get("StAgent_PERF_TOKEN", ""),
        help="Bearer token sent as Authorization header on every request.",
    )
    parser.add_argument("--provider", default="", help="Provider name recorded in the report.")
    parser.add_argument("--model", default="", help="Model name recorded in the report.")
    parser.add_argument(
        "--warmup",
        type=int,
        default=0,
        help="Throwaway requests before timing to absorb cold-start cost.",
    )
    terminal = parser.add_mutually_exclusive_group()
    terminal.add_argument(
        "--require-terminal-event",
        dest="require_terminal_event",
        action="store_true",
        default=True,
        help="Treat a 2xx stream without a 'data: [DONE]' terminator as a failure (default).",
    )
    terminal.add_argument(
        "--no-require-terminal-event",
        dest="require_terminal_event",
        action="store_false",
        help="Accept any 2xx stream as success (not recommended).",
    )
    parser.add_argument(
        "--min-success-rate",
        type=float,
        default=0.99,
        help="Minimum success rate a --baseline-output run must reach.",
    )
    parser.add_argument(
        "--baseline-output",
        default="",
        help=(
            "Write a measured baseline JSON here only if the run has >=100 "
            "requests, each stream terminated, and the success rate clears "
            "--min-success-rate; otherwise exit non-zero and write nothing."
        ),
    )
    args = parser.parse_args()

    result, all_samples = asyncio.run(_run(args))

    baseline_ok = False
    if args.baseline_output:
        baseline_ok, reason = _baseline_is_valid(
            all_samples,
            min_requests=100,
            min_success_rate=args.min_success_rate,
        )
        if not baseline_ok:
            print(f"baseline rejected: {reason}")
            raise SystemExit(2)

    result["evidence_kind"] = "measured_baseline" if baseline_ok else "run"

    text = json.dumps(result, ensure_ascii=False, indent=2)
    print(text)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.write("\n")
    if baseline_ok:
        # Atomic replace so a partial write never clobbers a trusted baseline.
        tmp = f"{args.baseline_output}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.write("\n")
        os.replace(tmp, args.baseline_output)


if __name__ == "__main__":
    main()
