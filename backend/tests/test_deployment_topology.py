import pytest

from StAgent.runtime.deployment_topology import (
    DeploymentTopologyError,
    validate_deployment_topology,
)


def test_sqlite_rejects_multiple_workers() -> None:
    with pytest.raises(DeploymentTopologyError, match="SQLite"):
        validate_deployment_topology(database_backend="sqlite", workers=2)


def test_sqlite_accepts_one_worker() -> None:
    report = validate_deployment_topology(database_backend="sqlite", workers=1)
    assert report.safe is True
    assert report.workers == 1


def test_rejects_zero_workers() -> None:
    with pytest.raises(DeploymentTopologyError):
        validate_deployment_topology(database_backend="sqlite", workers=0)


def test_postgres_multiworker_keeps_explicit_boundary_warning() -> None:
    report = validate_deployment_topology(
        database_backend="postgresql",
        workers=4,
        redis_enabled=True,
    )
    assert report.safe is True
    assert "sticky sessions" in " ".join(report.warnings).lower()
    assert "event bus" in " ".join(report.warnings).lower()


def test_postgres_multiworker_rejects_missing_shared_run_store() -> None:
    with pytest.raises(DeploymentTopologyError, match="StAgent_REDIS_ENABLED"):
        validate_deployment_topology(database_backend="postgresql", workers=2)


def test_cli_start_rejects_sqlite_multiworker(monkeypatch: pytest.MonkeyPatch) -> None:
    """`StAgent app start --workers 2` must fail before launching a server."""
    from click.testing import CliRunner

    from StAgent.cli import app_cmd

    called = {"uvicorn": False, "gunicorn": False}
    monkeypatch.setattr(
        app_cmd, "_run_uvicorn", lambda *a, **k: called.__setitem__("uvicorn", True)
    )
    monkeypatch.setattr(
        app_cmd, "_run_gunicorn", lambda *a, **k: called.__setitem__("gunicorn", True)
    )

    result = CliRunner().invoke(app_cmd.app_group, ["start", "--workers", "2"])

    assert result.exit_code != 0
    assert "SQLite" in result.output
    assert called == {"uvicorn": False, "gunicorn": False}


def _repo_root():
    from pathlib import Path

    return Path(__file__).resolve().parents[2]


def test_start_ps1_has_no_hardcoded_four_workers() -> None:
    text = (_repo_root() / "start.ps1").read_text(encoding="utf-8")
    assert "--workers 4" not in text
    assert "StAgent_WORKERS" in text


def test_start_sh_has_no_hardcoded_four_workers() -> None:
    text = (_repo_root() / "start.sh").read_text(encoding="utf-8")
    assert "--workers 4" not in text
    assert "StAgent_WORKERS" in text
