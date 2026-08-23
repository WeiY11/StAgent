#!/usr/bin/env python
"""Run the default backend pytest suite in progress-visible batches.

``pytest tests`` is correct semantically, but on local Windows runs it can look
stuck for minutes and hit external watchdog timeouts before it reports the file
that failed. This wrapper keeps the same default contract (non-live tests only)
while splitting the suite into deterministic, named batches.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence


@dataclass(frozen=True, slots=True)
class TestBatch:
    name: str
    paths: tuple[str, ...]


TOP_LEVEL_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "top_agent_api",
        (
            r"^test_agent",
            r"^test_anthropic",
            r"^test_api",
            r"^test_approval",
            r"^test_artifact",
            r"^test_attachment",
            r"^test_auth",
            r"^test_autocompact",
            r"^test_auto_review",
            r"^test_bm25",
        ),
    ),
    ("top_cache", (r"^test_cache",)),
    (
        "top_canvas_catalog",
        (r"^test_canvas", r"^test_catalog", r"^test_change", r"^test_channels"),
    ),
    ("top_chat", (r"^test_chat",)),
    ("top_code_coding", (r"^test_code", r"^test_coding")),
    (
        "top_config_cron_custom",
        (r"^test_ci", r"^test_config", r"^test_content", r"^test_cron", r"^test_custom"),
    ),
    ("top_d_to_f", (r"^test_[def]",)),
    ("top_g_to_k", (r"^test_[ghijk]",)),
    ("top_l_to_m", (r"^test_[lm]",)),
    ("top_n_to_o", (r"^test_[no]",)),
    ("top_p_path_pdf", (r"^test_path", r"^test_pdf", r"^test_perf")),
    (
        "top_p_pet_procedure",
        (
            r"^test_pet",
            r"^test_plan",
            r"^test_playbook",
            r"^test_plugin",
            r"^test_polite",
            r"^test_procedure",
            r"^test_progress",
        ),
    ),
    (
        "top_p_project_python",
        (r"^test_project", r"^test_prompts", r"^test_provider", r"^test_python"),
    ),
    ("top_q_to_r", (r"^test_[qr]",)),
    (
        "top_s_session",
        (r"^test_schema", r"^test_sdk", r"^test_security", r"^test_services", r"^test_session"),
    ),
    (
        "top_s_settings_skills",
        (
            r"^test_settings",
            r"^test_skills",
            r"^test_smtp",
            r"^test_sqlite",
            r"^test_stats",
            r"^test_steer",
            r"^test_subagent",
            r"^test_syntax",
        ),
    ),
    ("top_task", (r"^test_task",)),
    (
        "top_tools_workflow",
        (
            r"^test_tools",
            r"^test_tool",
            r"^test_trace",
            r"^test_utils",
            r"^test_uv",
            r"^test_verification",
            r"^test_vllm",
            r"^test_web",
            r"^test_workflow",
            r"^test_worktree",
        ),
    ),
)

DIRECTORY_BATCHES: tuple[tuple[str, str], ...] = (
    ("dir_eval", "tests/eval"),
    ("dir_file", "tests/file"),
    ("dir_integration", "tests/integration"),
    ("dir_llm", "tests/llm"),
    ("dir_channels_context", "tests/test_channels"),
    ("dir_context", "tests/test_context"),
    ("dir_docgen", "tests/test_docgen"),
    ("dir_file_invariants", "tests/test_file"),
    ("dir_library", "tests/test_library"),
    ("dir_tools", "tests/test_tools"),
    ("dir_workflow", "tests/workflow"),
)


def _rel(path: Path, backend_root: Path) -> str:
    return path.relative_to(backend_root).as_posix()


def _matching(files: Sequence[Path], patterns: Sequence[str]) -> list[Path]:
    compiled = tuple(re.compile(pattern) for pattern in patterns)
    return [path for path in files if any(pattern.search(path.name) for pattern in compiled)]


def build_batches(backend_root: Path | None = None) -> list[TestBatch]:
    root = (backend_root or Path(__file__).resolve().parents[1]).resolve()
    tests_root = root / "tests"
    top_level = sorted(tests_root.glob("test_*.py"))
    batches: list[TestBatch] = []

    for name, rel_path in DIRECTORY_BATCHES:
        path = root / rel_path
        if path.exists():
            batches.append(TestBatch(name=name, paths=(rel_path,)))

    assigned: set[Path] = set()
    for name, patterns in TOP_LEVEL_GROUPS:
        paths = [path for path in _matching(top_level, patterns) if path not in assigned]
        if not paths:
            continue
        assigned.update(paths)
        batches.append(TestBatch(name=name, paths=tuple(_rel(path, root) for path in paths)))

    remainder = [path for path in top_level if path not in assigned]
    if remainder:
        batches.append(
            TestBatch(
                name="top_other",
                paths=tuple(_rel(path, root) for path in remainder),
            )
        )
    return batches


def pytest_command(batch: TestBatch, pytest_args: Sequence[str]) -> list[str]:
    return [sys.executable, "-m", "pytest", *batch.paths, *pytest_args]


def coverage_pytest_args(pytest_args: Sequence[str], *, package: str) -> list[str]:
    return [f"--cov={package}", "--cov-append", "--cov-report=", *pytest_args]


def coverage_xml_command(output_path: Path) -> list[str]:
    return [sys.executable, "-m", "coverage", "xml", "-o", str(output_path)]


def coverage_report_command(fail_under: float | None) -> list[str]:
    command = [sys.executable, "-m", "coverage", "report"]
    if fail_under is not None:
        threshold = str(int(fail_under)) if fail_under.is_integer() else str(fail_under)
        command.append(f"--fail-under={threshold}")
    return command


def _resolve_backend_path(backend_root: Path, path_text: str) -> Path:
    path = Path(path_text)
    if not path.is_absolute():
        path = backend_root / path
    resolved = path.resolve()
    try:
        resolved.relative_to(backend_root)
    except ValueError as exc:
        raise SystemExit(f"path must stay under backend root: {path_text}") from exc
    return resolved


def _remove_previous_coverage(backend_root: Path, xml_path: Path) -> None:
    for path in (backend_root / ".coverage", xml_path):
        if path.exists():
            path.unlink()

    for path in backend_root.glob(".coverage.*"):
        resolved = path.resolve()
        try:
            resolved.relative_to(backend_root)
        except ValueError as exc:
            raise SystemExit(f"refusing to remove coverage file outside backend root: {path}") from exc
        if resolved.is_file():
            resolved.unlink()


def _validate_no_pytest_coverage_args(pytest_args: Sequence[str]) -> None:
    forbidden_prefixes = ("--cov", "--cov-report", "--cov-fail-under")
    forbidden = [
        arg
        for arg in pytest_args
        if arg in forbidden_prefixes or any(arg.startswith(prefix + "=") for prefix in forbidden_prefixes)
    ]
    if forbidden:
        raise SystemExit(
            "do not pass pytest-cov flags after '--' when wrapper --cov is enabled; "
            f"use wrapper options instead: {', '.join(forbidden)}"
        )


def _select_batches(batches: Sequence[TestBatch], names: Sequence[str]) -> list[TestBatch]:
    if not names:
        return list(batches)
    by_name = {batch.name: batch for batch in batches}
    missing = [name for name in names if name not in by_name]
    if missing:
        available = ", ".join(sorted(by_name))
        raise SystemExit(f"unknown batch name(s): {', '.join(missing)}\navailable: {available}")
    return [by_name[name] for name in names]


def _run_batch(batch: TestBatch, pytest_args: Sequence[str], *, env: dict[str, str]) -> int:
    command = pytest_command(batch, pytest_args)
    started = time.perf_counter()
    print(f"\n=== {batch.name} ({len(batch.paths)} path(s)) ===", flush=True)
    print(" ".join(command), flush=True)
    proc = subprocess.run(command, cwd=Path(__file__).resolve().parents[1], env=env, check=False)
    elapsed = time.perf_counter() - started
    print(f"=== {batch.name} exit={proc.returncode} elapsed={elapsed:.1f}s ===", flush=True)
    return proc.returncode


def _run_command(command: Sequence[str], *, cwd: Path, env: dict[str, str], label: str) -> int:
    started = time.perf_counter()
    print(f"\n=== {label} ===", flush=True)
    print(" ".join(command), flush=True)
    proc = subprocess.run(command, cwd=cwd, env=env, check=False)
    elapsed = time.perf_counter() - started
    print(f"=== {label} exit={proc.returncode} elapsed={elapsed:.1f}s ===", flush=True)
    return proc.returncode


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="Print batch names and paths, then exit.")
    parser.add_argument(
        "--batch",
        action="append",
        default=[],
        help="Run only this batch name. May be passed more than once.",
    )
    parser.add_argument(
        "--fail-fast",
        action="store_true",
        help="Stop after the first failing batch.",
    )
    parser.add_argument(
        "--cov",
        action="store_true",
        help="Collect one combined coverage dataset across all selected batches.",
    )
    parser.add_argument(
        "--cov-package",
        default="StAgent",
        help="Package passed to pytest-cov when --cov is enabled.",
    )
    parser.add_argument(
        "--cov-fail-under",
        type=float,
        default=None,
        help="Run coverage report with this combined fail-under threshold after all batches pass.",
    )
    parser.add_argument(
        "--cov-report-xml",
        default="coverage.xml",
        help="Coverage XML output path under the backend root when --cov is enabled.",
    )
    parser.add_argument(
        "pytest_args",
        nargs=argparse.REMAINDER,
        help="Extra pytest args after '--'. Defaults to '-q'.",
    )
    args = parser.parse_args(argv)

    backend_root = Path(__file__).resolve().parents[1]
    batches = build_batches(backend_root)
    selected = _select_batches(batches, args.batch)

    if args.list:
        for batch in selected:
            print(f"{batch.name}: {' '.join(batch.paths)}")
        return 0

    pytest_args = list(args.pytest_args)
    if pytest_args and pytest_args[0] == "--":
        pytest_args = pytest_args[1:]
    if not pytest_args:
        pytest_args = ["-q"]
    if args.cov:
        _validate_no_pytest_coverage_args(pytest_args)
        xml_path = _resolve_backend_path(backend_root, args.cov_report_xml)
        _remove_previous_coverage(backend_root, xml_path)
        pytest_args = coverage_pytest_args(pytest_args, package=args.cov_package)

    env = dict(os.environ)
    env.setdefault("PYTHONUNBUFFERED", "1")
    env.setdefault("StAgent_SECURITY_ENFORCE_AUTH", "false")
    env.setdefault("StAgent_SECURITY_RATE_LIMIT_ENABLED", "false")

    failed: list[str] = []
    total_started = time.perf_counter()
    for batch in selected:
        code = _run_batch(batch, pytest_args, env=env)
        if code != 0:
            failed.append(batch.name)
            if args.fail_fast:
                break

    elapsed = time.perf_counter() - total_started
    if failed:
        print(f"\nFAILED batches ({len(failed)}): {', '.join(failed)}", flush=True)
        return 1
    if args.cov:
        xml_code = _run_command(
            coverage_xml_command(xml_path),
            cwd=backend_root,
            env=env,
            label="coverage xml",
        )
        if xml_code != 0:
            return xml_code
        report_code = _run_command(
            coverage_report_command(args.cov_fail_under),
            cwd=backend_root,
            env=env,
            label="coverage report",
        )
        if report_code != 0:
            return report_code
    print(f"\nAll {len(selected)} batch(es) passed in {elapsed:.1f}s.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
