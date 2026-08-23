"""Contracts for the progress-visible default backend test runner."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import ModuleType

_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "run_default_tests.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("run_default_tests", _SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["run_default_tests"] = module
    spec.loader.exec_module(module)
    return module


runner = _load()


def test_batches_cover_current_top_level_test_files_once() -> None:
    backend_root = Path(__file__).resolve().parents[1]
    expected = {
        path.relative_to(backend_root).as_posix()
        for path in (backend_root / "tests").glob("test_*.py")
    }
    batches = runner.build_batches(backend_root)
    actual: list[str] = [
        path
        for batch in batches
        if batch.name.startswith("top_")
        for path in batch.paths
    ]

    assert set(actual) == expected
    assert len(actual) == len(set(actual))


def test_batches_keep_heavy_chat_and_code_groups_separate() -> None:
    batches = {batch.name: set(batch.paths) for batch in runner.build_batches()}

    assert "tests/test_chat_agent_memory_api.py" in batches["top_chat"]
    assert "tests/test_chat_delete_session_hard.py" in batches["top_chat"]
    assert "tests/test_code_execution.py" in batches["top_code_coding"]
    assert "tests/test_coding_projects_api.py" in batches["top_code_coding"]


def test_pytest_command_uses_current_interpreter() -> None:
    batch = runner.TestBatch(name="sample", paths=("tests/test_live_test_gating.py",))

    command = runner.pytest_command(batch, ["-q"])

    assert command[:3] == [sys.executable, "-m", "pytest"]
    assert command[-1] == "-q"


def test_coverage_pytest_args_append_without_batch_fail_under() -> None:
    args = runner.coverage_pytest_args(["-q", "--durations=40"], package="StAgent")

    assert args[:3] == ["--cov=StAgent", "--cov-append", "--cov-report="]
    assert args[-2:] == ["-q", "--durations=40"]
    assert not any(arg.startswith("--cov-fail-under") for arg in args)


def test_coverage_report_command_formats_integer_threshold() -> None:
    command = runner.coverage_report_command(60.0)

    assert command == [sys.executable, "-m", "coverage", "report", "--fail-under=60"]


def test_wrapper_cov_rejects_pytest_cov_passthrough() -> None:
    try:
        runner._validate_no_pytest_coverage_args(["-q", "--cov=StAgent"])
    except SystemExit as exc:
        assert "wrapper --cov" in str(exc)
    else:
        raise AssertionError("expected SystemExit for pytest-cov passthrough")
