"""Static contracts for Docker and release smoke-test wiring.

These tests do not build the image. They pin the local deployment files to the
same runtime assumptions used by the CI smoke gate.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEPLOY_DIR = _REPO_ROOT / "deploy"
_WORKFLOW_DIR = _REPO_ROOT / ".github" / "workflows"


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _load_yaml(path: Path) -> dict[str, Any]:
    return yaml.safe_load(_read_text(path))


def test_dockerfile_healthcheck_tracks_runtime_port_and_readiness() -> None:
    dockerfile = _read_text(_DEPLOY_DIR / "Dockerfile")

    assert "FROM python:3.11-slim-bookworm AS production" in dockerfile
    assert 'ENTRYPOINT ["tini", "--", "/entrypoint.sh"]' in dockerfile
    assert "CMD [\"server\"]" in dockerfile
    assert "HEALTHCHECK" in dockerfile
    assert "localhost:${LEAGENT_PORT:-8000}/api/v1/health/ready" in dockerfile
    assert "localhost:8000/health" not in dockerfile


def test_compose_requires_secret_and_uses_same_readiness_endpoint() -> None:
    compose = _load_yaml(_DEPLOY_DIR / "docker-compose.yml")
    service = compose["services"]["leagent"]
    environment = service["environment"]

    assert service["build"]["target"] == "${BUILD_TARGET:-production}"
    assert environment["LEAGENT_HOME"] == "/app/data"
    assert environment["LEAGENT_PORT"] == "8000"
    assert environment["LEAGENT_SECRET_KEY"].startswith("${LEAGENT_SECRET_KEY:?")
    assert environment["LEAGENT_FILES__SIGNED_URL_SECRET"] == "${LEAGENT_SECRET_KEY}"
    assert environment["LEAGENT_RUN_MIGRATIONS"] == "${LEAGENT_RUN_MIGRATIONS:-true}"
    assert environment["LEAGENT_MIGRATIONS_REQUIRED"] == "${LEAGENT_MIGRATIONS_REQUIRED:-true}"
    assert environment["DATABASE_URL"] == "${DATABASE_URL:-}"
    assert environment["DB_DATABASE_URL"] == "${DB_DATABASE_URL:-}"
    assert environment["DB_SQLITE_PATH"] == "${DB_SQLITE_PATH:-}"
    assert environment["DB_POOL_SIZE"] == "${DB_POOL_SIZE:-5}"
    assert environment["DB_MAX_OVERFLOW"] == "${DB_MAX_OVERFLOW:-10}"
    assert service["healthcheck"]["test"] == [
        "CMD-SHELL",
        'curl -f "http://localhost:$${LEAGENT_PORT:-8000}/api/v1/health/ready"',
    ]


def test_docker_runtime_contract_documents_postgres_and_sqlite_boundaries() -> None:
    entrypoint = _read_text(_DEPLOY_DIR / "entrypoint.sh")
    helper = _read_text(_DEPLOY_DIR / "run_docker.sh")
    readme = _read_text(_DEPLOY_DIR / "README.md")
    env_example = _read_text(_DEPLOY_DIR / ".env.example")

    assert "LEAGENT_WORKERS > 1 requires PostgreSQL DATABASE_URL" in entrypoint
    assert "postgres://*|postgresql://*|postgresql+*" in entrypoint
    assert "DATABASE_URL=postgresql+asyncpg://" in env_example
    assert "Set `DATABASE_URL` to use an external PostgreSQL database" in readme
    assert "Use pg_dump/pg_restore for PostgreSQL" in helper
    assert "PostgreSQL  " in helper


def test_dev_compose_uses_readiness_endpoint() -> None:
    compose = _load_yaml(_DEPLOY_DIR / "docker-compose.dev.yml")
    service = compose["services"]["leagent"]

    assert service["healthcheck"]["test"] == [
        "CMD",
        "curl",
        "-f",
        "http://localhost:8000/api/v1/health/ready",
    ]


def test_entrypoint_and_docker_helper_health_use_readiness() -> None:
    entrypoint = _read_text(_DEPLOY_DIR / "entrypoint.sh")
    helper = _read_text(_DEPLOY_DIR / "run_docker.sh")

    assert 'http://localhost:${LEAGENT_PORT}/api/v1/health/ready' in entrypoint
    assert 'http://localhost:${LEAGENT_PORT}/health' not in entrypoint
    assert "http://localhost:$port/api/v1/health/ready" in helper
    assert "http://localhost:$port/health" not in helper


def test_supervisor_bind_tracks_entrypoint_host_and_port() -> None:
    supervisor = _read_text(_DEPLOY_DIR / "supervisord.conf")

    assert "--bind %(ENV_LEAGENT_HOST)s:%(ENV_LEAGENT_PORT)s" in supervisor
    assert "--bind 0.0.0.0:8000" not in supervisor


def test_ci_docker_smoke_covers_startup_ready_and_postgres_driver() -> None:
    ci = _load_yaml(_WORKFLOW_DIR / "ci.yml")
    job = ci["jobs"]["docker-build"]

    build_step = next(step for step in job["steps"] if step.get("name") == "Build Docker image")
    assert build_step["with"]["target"] == "production"

    run_blocks = "\n".join(str(step.get("run", "")) for step in job["steps"])
    assert "-p 127.0.0.1:18000:8000" in run_blocks
    assert "LEAGENT_SECRET_KEY=test-secret-key-with-at-least-32-bytes" in run_blocks
    assert "for path in /health /api/v1/health/ready" in run_blocks
    assert 'curl -fsS "http://127.0.0.1:18000${path}"' in run_blocks
    assert "import leagent, asyncpg" in run_blocks
