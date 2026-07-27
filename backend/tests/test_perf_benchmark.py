"""Unit tests for the perf benchmark's HTTP/SSE truth rules.

The benchmark lives in ``backend/scripts`` (not an installed package), so it is
loaded by file path here.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import ModuleType

_PERF_PATH = Path(__file__).resolve().parents[1] / "scripts" / "perf_benchmark.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("perf_benchmark", _PERF_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolves type hints via sys.modules, so register first.
    sys.modules["perf_benchmark"] = module
    spec.loader.exec_module(module)
    return module


perf = _load()
Sample = perf.Sample
_classify_error = perf._classify_error
_sample_from_stream = perf._sample_from_stream


def test_http_404_is_failure() -> None:
    sample = Sample(
        scenario="chat_stream",
        ok=False,
        status=404,
        first_event_ms=0,
        total_ms=1,
        bytes_read=0,
        terminal_event_seen=False,
    )
    assert _classify_error(sample) == "http_4xx"


def test_stream_without_done_is_failure() -> None:
    sample = _sample_from_stream(
        status=200,
        lines=[b"event: content\n", b'data: {"token":"x"}\n'],
    )
    assert sample.ok is False
    assert sample.error == "missing_terminal_event"


def test_done_stream_is_success() -> None:
    sample = _sample_from_stream(
        status=200,
        lines=[b"event: message\n", b"data: [DONE]\n"],
    )
    assert sample.ok is True
    assert sample.terminal_event_seen is True


def test_baseline_rejects_too_few_requests() -> None:
    samples = [
        _sample_from_stream(status=200, lines=[b"data: [DONE]\n"]) for _ in range(10)
    ]
    ok, reason = perf._baseline_is_valid(samples, min_requests=100, min_success_rate=0.99)
    assert ok is False
    assert "requests" in reason


def test_baseline_rejects_low_success_rate() -> None:
    good = [
        _sample_from_stream(status=200, lines=[b"data: [DONE]\n"]) for _ in range(90)
    ]
    bad = [
        _sample_from_stream(status=200, lines=[b'data: {"t":"x"}\n']) for _ in range(30)
    ]
    ok, reason = perf._baseline_is_valid(good + bad, min_requests=100, min_success_rate=0.99)
    assert ok is False
    assert "success" in reason


def test_baseline_accepts_clean_run() -> None:
    samples = [
        _sample_from_stream(status=200, lines=[b"data: [DONE]\n"]) for _ in range(120)
    ]
    ok, reason = perf._baseline_is_valid(samples, min_requests=100, min_success_rate=0.99)
    assert ok is True
    assert reason == ""
