"""Machine-readable micro-benchmark for CacheService hot/cold/invalidated paths.

Emits a single JSON object so results can be diffed across revisions. Latency
numbers are *local evidence only* — never commit machine-specific p50/p95 as a
universal target. The load-bearing invariant is ``factory_calls == 1`` for the
hot path (single-flight + cache hits collapse repeated work onto one fill).

Usage::

    uv run python tests/bench_cache_hot_paths.py --iterations 1000
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time

from StAgent.config.settings import Settings
from StAgent.services.cache.service import CacheService


async def _measure(iterations: int) -> dict[str, float | int]:
    service = CacheService(Settings())
    await service.start()
    timings: list[float] = []
    calls = 0

    async def factory() -> dict[str, int]:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.005)
        return {"value": 1}

    for _ in range(iterations):
        started = time.perf_counter()
        await service.get_or_set("hot", factory, namespace="chat")
        timings.append((time.perf_counter() - started) * 1_000)
    await service.stop()
    ordered = sorted(timings)
    return {
        "iterations": iterations,
        "factory_calls": calls,
        "p50_ms": statistics.median(ordered),
        "p95_ms": ordered[max(0, int(len(ordered) * 0.95) - 1)],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=1_000)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(_measure(args.iterations)), sort_keys=True))
