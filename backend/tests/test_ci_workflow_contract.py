"""Contracts for repository CI workflows.

These tests keep the expensive backend test gate aligned with the local
progress-visible runner. They do not execute GitHub Actions; they pin the YAML
contract that controls the remote gate.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOW_DIR = _REPO_ROOT / ".github" / "workflows"
_DESKTOP_SCRIPT_DIR = _REPO_ROOT / "desktop" / "scripts"


def _load_workflow(name: str) -> dict[str, Any]:
    return yaml.safe_load((_WORKFLOW_DIR / name).read_text(encoding="utf-8"))


def _run_step(job: dict[str, Any], step_name: str) -> str:
    for step in job["steps"]:
        if step.get("name") == step_name:
            return str(step["run"])
    raise AssertionError(f"step not found: {step_name}")


def _needs(job: dict[str, Any]) -> list[str]:
    needs = job.get("needs", [])
    if isinstance(needs, str):
        return [needs]
    return list(needs)


def _steps_using(job: dict[str, Any], needle: str) -> list[dict[str, Any]]:
    return [step for step in job["steps"] if needle in str(step)]


def _step(job: dict[str, Any], name: str) -> dict[str, Any]:
    for step in job["steps"]:
        if step.get("name") == name:
            return step
    raise AssertionError(f"step not found: {name}")


def test_ci_backend_tests_use_batched_runner_with_combined_coverage() -> None:
    job = _load_workflow("ci.yml")["jobs"]["backend-test"]
    run = _run_step(job, "Run tests")

    assert int(job["timeout-minutes"]) >= 45
    assert "uv run python scripts/run_default_tests.py" in run
    assert "--fail-fast" in run
    assert "--cov --cov-fail-under 60 --cov-report-xml coverage.xml" in run
    assert "--durations=40" in run
    assert "uv run pytest tests/" not in run


def test_ci_coverage_and_artifact_uploads_are_file_gated() -> None:
    workflow = _load_workflow("ci.yml")
    backend_coverage = _step(workflow["jobs"]["backend-test"], "Upload coverage")
    frontend_coverage = _step(workflow["jobs"]["frontend-test"], "Upload coverage")
    frontend_artifact = _step(workflow["jobs"]["frontend-build"], "Upload build artifacts")

    assert backend_coverage["if"] == "always() && hashFiles('backend/coverage.xml') != ''"
    assert backend_coverage["with"]["files"] == "backend/coverage.xml"
    assert frontend_coverage["if"] == "always() && hashFiles('frontend/coverage/lcov.info') != ''"
    assert frontend_coverage["with"]["files"] == "frontend/coverage/lcov.info"
    assert frontend_artifact["with"]["path"] == "frontend/dist/"
    assert frontend_artifact["with"]["if-no-files-found"] == "error"


def test_ci_fast_contracts_cover_manifest_and_runtime_contracts() -> None:
    job = _load_workflow("ci.yml")["jobs"]["fast-contracts"]
    run = _run_step(job, "Run fast contracts")

    assert "continue-on-error" not in str(job)
    for path in (
        "tests/test_api_manifest.py",
        "tests/test_router.py",
        "tests/test_openapi_schema.py",
        "tests/test_execution_topology_invariants.py",
        "tests/test_execution_run_registry_lifecycle.py",
    ):
        assert path in run


def test_ci_detects_and_tests_desktop_changes() -> None:
    workflow = _load_workflow("ci.yml")
    changes = workflow["jobs"]["changes"]
    desktop_job = workflow["jobs"]["desktop-test"]
    filters = str(changes["steps"][1]["with"]["filters"])

    assert "desktop:" in filters
    assert "- 'desktop/**'" in filters
    assert changes["outputs"]["desktop"] == "${{ steps.filter.outputs.desktop }}"
    assert _needs(desktop_job) == ["changes"]
    assert "needs.changes.outputs.desktop == 'true'" in str(desktop_job["if"])
    assert "needs.changes.outputs.ci == 'true'" in str(desktop_job["if"])

    run_steps = {
        step.get("name"): step
        for step in desktop_job["steps"]
        if step.get("run")
    }
    assert run_steps["Install dependencies"]["run"] == "npm ci"
    assert run_steps["Install dependencies"]["working-directory"] == "desktop/electron"
    assert run_steps["Build Electron main process"]["run"] == "npm run build"
    assert run_steps["Run Electron unit tests"]["run"] == "npm test"


def test_ci_blocking_quality_gates_are_not_advisory() -> None:
    job = _load_workflow("ci.yml")["jobs"]["backend-lint"]

    blocking_steps = [
        step
        for step in job["steps"]
        if str(step.get("name", "")).startswith(("Ruff gate ", "Mypy "))
    ]
    assert len(blocking_steps) == 2
    assert all(step.get("continue-on-error") is not True for step in blocking_steps)

    advisory_steps = [step for step in job["steps"] if "advisory" in str(step.get("name", "")).lower()]
    assert advisory_steps
    assert all(step.get("continue-on-error") is True for step in advisory_steps)


def test_ci_docker_smoke_waits_for_all_quality_gates() -> None:
    job = _load_workflow("ci.yml")["jobs"]["docker-build"]

    assert set(_needs(job)) == {
        "changes",
        "backend-lint",
        "fast-contracts",
        "backend-test",
        "postgres-contract",
        "security-scan",
        "frontend-lint",
        "frontend-build",
        "frontend-test",
        "desktop-test",
    }
    condition = str(job["if"])
    assert "always()" in condition
    assert "!failure()" in condition
    assert "!cancelled()" in condition
    assert "needs.changes.outputs.backend == 'true'" in condition
    assert "needs.changes.outputs.ci == 'true'" in condition


def test_ci_security_scan_is_blocking_and_change_scoped() -> None:
    job = _load_workflow("ci.yml")["jobs"]["security-scan"]

    assert _needs(job) == ["changes"]
    job_condition = str(job["if"])
    assert "needs.changes.outputs.backend == 'true'" in job_condition
    assert "needs.changes.outputs.frontend == 'true'" in job_condition
    assert "needs.changes.outputs.ci == 'true'" in job_condition
    assert "continue-on-error" not in str(job)
    assert "|| echo" not in str(job)

    run_steps = {
        step.get("name"): step
        for step in job["steps"]
        if step.get("run")
    }
    assert run_steps["Export Python production requirements"]["run"] == (
        "uv export --frozen --no-hashes --no-dev --no-emit-project --output-file requirements-audit.txt"
    )
    assert run_steps["Export Python production requirements"]["working-directory"] == "backend"
    assert run_steps["Audit Python production dependencies"]["run"] == (
        "pip-audit --requirement requirements-audit.txt --desc"
    )
    assert run_steps["Audit Python production dependencies"]["working-directory"] == "backend"
    assert run_steps["Audit npm dependencies"]["run"] == "npm audit --audit-level=critical"
    assert run_steps["Audit npm dependencies"]["working-directory"] == "frontend"


def test_release_jobs_that_read_version_outputs_need_validate_version_directly() -> None:
    workflow = _load_workflow("release.yml")

    for job_name, job in workflow["jobs"].items():
        if _steps_using(job, "needs.validate-version.outputs."):
            assert "validate-version" in _needs(job), job_name


def test_release_backend_tests_use_same_batched_runner() -> None:
    job = _load_workflow("release.yml")["jobs"]["backend-tests"]
    run = _run_step(job, "Run tests")

    assert int(job["timeout-minutes"]) >= 45
    assert "uv run python scripts/run_default_tests.py" in run
    assert "--fail-fast" in run
    assert "--durations=40" in run
    assert "uv run pytest tests/" not in run


def test_release_backend_quality_reuses_ci_blocking_contracts() -> None:
    release_jobs = _load_workflow("release.yml")["jobs"]
    ci_jobs = _load_workflow("ci.yml")["jobs"]
    job = release_jobs["backend-quality"]

    assert _needs(job) == ["validate-version"]
    assert "continue-on-error" not in str(job)
    assert _run_step(job, "Install dependencies") == "uv sync --extra dev"
    assert _run_step(
        job,
        "Ruff gate (fatal + security + isort + pyupgrade + bugbear + perf/c4/pie/ret)",
    ) == _run_step(
        ci_jobs["backend-lint"],
        "Ruff gate (fatal + security + isort + pyupgrade + bugbear + perf/c4/pie/ret)",
    )

    release_mypy = next(step for step in job["steps"] if str(step.get("name", "")).startswith("Mypy"))
    ci_mypy = next(
        step
        for step in ci_jobs["backend-lint"]["steps"]
        if str(step.get("name", "")).startswith("Mypy")
    )
    assert release_mypy["run"] == ci_mypy["run"]

    release_fast_contracts = _run_step(job, "Run fast contracts")
    ci_fast_contracts = _run_step(ci_jobs["fast-contracts"], "Run fast contracts")
    assert release_fast_contracts == ci_fast_contracts
    for path in (
        "tests/test_api_manifest.py",
        "tests/test_openapi_schema.py",
        "tests/test_execution_topology_invariants.py",
    ):
        assert path in release_fast_contracts


def test_release_postgres_contract_gates_artifacts() -> None:
    jobs = _load_workflow("release.yml")["jobs"]
    job = jobs["postgres-contract"]

    assert _needs(job) == ["validate-version"]
    assert "continue-on-error" not in str(job)
    assert job["services"]["postgres"]["image"] == "postgres:17-alpine"
    assert job["services"]["postgres"]["env"] == {
        "POSTGRES_USER": "leagent",
        "POSTGRES_PASSWORD": "test",
        "POSTGRES_DB": "leagent",
    }
    assert "pg_isready -U leagent" in str(job["services"]["postgres"]["options"])
    assert job["env"]["DATABASE_URL"] == "postgresql+asyncpg://leagent:test@127.0.0.1:5432/leagent"
    assert job["env"]["SECRET_KEY"] == "test-secret-key"

    run_steps = [step for step in job["steps"] if step.get("run")]
    assert [step["run"] for step in run_steps] == [
        "uv sync --extra dev --extra postgres",
        "uv run pytest tests/integration/test_postgres_smoke.py -q --run-live",
    ]
    assert all(step["working-directory"] == "backend" for step in run_steps)

    for job_name in ("build-wheel", "desktop-linux", "desktop-macos", "desktop-windows"):
        assert "postgres-contract" in _needs(jobs[job_name]), job_name


def test_ci_postgres_contract_runs_explicit_live_bootstrap_smoke() -> None:
    job = _load_workflow("ci.yml")["jobs"]["postgres-contract"]

    assert "continue-on-error" not in str(job)
    assert job["services"]["postgres"]["image"] == "postgres:17-alpine"
    assert job["env"]["DATABASE_URL"] == "postgresql+asyncpg://leagent:test@127.0.0.1:5432/leagent"
    assert "pg_isready -U leagent" in str(job["services"]["postgres"]["options"])

    run_steps = [step for step in job["steps"] if step.get("run")]
    assert [step["run"] for step in run_steps] == [
        "uv sync --extra dev --extra postgres",
        "uv run pytest tests/integration/test_postgres_smoke.py -q --run-live",
    ]
    assert all(step["working-directory"] == "backend" for step in run_steps)


def test_release_security_scan_is_blocking_and_unconditional() -> None:
    release_jobs = _load_workflow("release.yml")["jobs"]
    ci_job = _load_workflow("ci.yml")["jobs"]["security-scan"]
    job = release_jobs["security-scan"]

    assert _needs(job) == ["validate-version"]
    assert "continue-on-error" not in str(job)
    assert "|| echo" not in str(job)
    assert "needs.changes" not in str(job)

    run_steps = {
        step.get("name"): step
        for step in job["steps"]
        if step.get("run")
    }
    ci_run_steps = {
        step.get("name"): step
        for step in ci_job["steps"]
        if step.get("run")
    }
    for name in (
        "Export Python production requirements",
        "Audit Python production dependencies",
        "Audit npm dependencies",
    ):
        assert run_steps[name]["run"] == ci_run_steps[name]["run"]
        assert run_steps[name].get("working-directory") == ci_run_steps[name].get("working-directory")
    assert run_steps["Install pip-audit"]["run"] == "python -m pip install pip-audit"

    assert any(step.get("uses") == "actions/setup-node@v6" for step in job["steps"])
    for job_name in ("build-wheel", "desktop-linux", "desktop-macos", "desktop-windows"):
        assert "security-scan" in _needs(release_jobs[job_name]), job_name


def test_release_docker_smoke_reuses_ci_runtime_probe() -> None:
    release_jobs = _load_workflow("release.yml")["jobs"]
    ci_job = _load_workflow("ci.yml")["jobs"]["docker-build"]
    job = release_jobs["docker-smoke"]

    assert set(_needs(job)) == {
        "backend-quality",
        "backend-tests",
        "postgres-contract",
        "security-scan",
        "frontend-tests",
        "frontend-build",
    }
    assert "continue-on-error" not in str(job)

    release_build = _step(job, "Build Docker image")
    ci_build = _step(ci_job, "Build Docker image")
    assert release_build["uses"] == ci_build["uses"]
    assert release_build["with"] == ci_build["with"]

    for name in (
        "Start built image",
        "Wait for /health and /api/v1/health/ready",
        "Verify runtime imports inside the image",
    ):
        assert _run_step(job, name) == _run_step(ci_job, name)

    cleanup = _step(job, "Container logs and cleanup")
    assert cleanup["if"] == "always()"
    assert "docker logs leagent-smoke" in cleanup["run"]
    assert "docker rm -f leagent-smoke" in cleanup["run"]

    for job_name in ("build-wheel", "desktop-linux", "desktop-macos", "desktop-windows"):
        assert "docker-smoke" in _needs(release_jobs[job_name]), job_name


def test_release_frontend_build_gates_downstream_artifacts() -> None:
    workflow = _load_workflow("release.yml")
    jobs = workflow["jobs"]
    frontend_build = jobs["frontend-build"]

    assert _needs(frontend_build) == ["validate-version"]
    assert _run_step(frontend_build, "Build") == "npm run build"
    assert _run_step(frontend_build, "Check bundle budget") == "npm run check:bundle"
    verify_css = _run_step(frontend_build, "Verify non-empty CSS in dist")
    assert "test -d frontend/dist" in verify_css
    assert "find frontend/dist" in verify_css

    for job_name in ("build-wheel", "desktop-linux", "desktop-macos", "desktop-windows"):
        assert "frontend-build" in _needs(jobs[job_name]), job_name


def test_release_artifact_builds_wait_for_all_quality_gates() -> None:
    jobs = _load_workflow("release.yml")["jobs"]
    artifact_gate_jobs = {
        "backend-quality",
        "backend-tests",
        "postgres-contract",
        "security-scan",
        "docker-smoke",
        "frontend-tests",
        "frontend-build",
    }

    assert set(_needs(jobs["build-wheel"])) == artifact_gate_jobs
    for job_name in ("desktop-linux", "desktop-macos", "desktop-windows"):
        assert set(_needs(jobs[job_name])) == artifact_gate_jobs | {"validate-version"}


def test_release_publishes_only_after_required_artifacts_exist() -> None:
    workflow = _load_workflow("release.yml")
    jobs = workflow["jobs"]

    assert set(_needs(jobs["publish-release"])) == {
        "validate-version",
        "build-wheel",
        "desktop-linux",
        "desktop-macos",
        "desktop-windows",
    }

    for job_name in ("build-wheel", "desktop-linux", "desktop-macos", "desktop-windows"):
        upload_steps = [step for step in jobs[job_name]["steps"] if step.get("uses") == "actions/upload-artifact@v7"]
        assert upload_steps, job_name
        assert all(step["with"].get("if-no-files-found") == "error" for step in upload_steps), job_name

    release_steps = jobs["publish-release"]["steps"]
    release_index = next(i for i, step in enumerate(release_steps) if step.get("name") == "Create draft release with all artifacts")
    pypi_index = next(i for i, step in enumerate(release_steps) if step.get("name") == "Publish to PyPI")
    flip_index = next(i for i, step in enumerate(release_steps) if step.get("name") == "Flip release to public")
    release_step = release_steps[release_index]

    assert release_index < pypi_index < flip_index
    assert release_step["with"]["draft"] is True
    assert release_step["with"]["fail_on_unmatched_files"] is True
    files = str(release_step["with"]["files"])
    for artifact_dir in (
        "dist/wheel/*",
        "dist/desktop-linux/*",
        "dist/desktop-macos/*",
        "dist/desktop-windows/*",
    ):
        assert artifact_dir in files


def test_desktop_beta_release_fails_when_packaged_artifacts_are_missing() -> None:
    workflow = _load_workflow("desktop-release.yml")
    job = workflow["jobs"]["build"]

    upload_step = next(step for step in job["steps"] if step.get("uses") == "actions/upload-artifact@v7")
    assert upload_step["with"]["if-no-files-found"] == "error"

    workflow_text = (_WORKFLOW_DIR / "desktop-release.yml").read_text(encoding="utf-8")
    assert "pypa/gh-action-pypi-publish" not in workflow_text


def test_desktop_packaging_scripts_run_unit_tests_before_packaging() -> None:
    for script_name in ("build-linux.sh", "build-mac.sh"):
        script = (_DESKTOP_SCRIPT_DIR / script_name).read_text(encoding="utf-8")
        assert script.index("npm run build") < script.index("npm test") < script.index("npx electron-builder")

    win_script = (_DESKTOP_SCRIPT_DIR / "build-win.ps1").read_text(encoding="utf-8")
    assert win_script.index("npm run build") < win_script.index("npm test") < win_script.index("npx electron-builder")
    assert 'throw "npm test failed' in win_script
