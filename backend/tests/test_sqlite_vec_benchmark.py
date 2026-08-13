"""Contract test for the reproducible sqlite-vec scaling benchmark."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from types import ModuleType

_BENCHMARK_PATH = Path(__file__).resolve().parents[1] / "scripts" / "bench_sqlite_vec.py"


def _load_benchmark() -> ModuleType:
    spec = importlib.util.spec_from_file_location("bench_sqlite_vec", _BENCHMARK_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["bench_sqlite_vec"] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_benchmark_inserts_and_searches_requested_rows() -> None:
    benchmark = _load_benchmark()
    result = await benchmark.run_benchmark(rows=64, dimension=8, batch_size=16, limit=5)

    assert result["available"] is True
    assert result["rows_requested"] == 64
    assert result["rows_inserted"] == 64
    assert result["hits"] == 5
    assert result["database_bytes"] > 0
