"""Deployment topology safety contract.

The default deployment is SQLite + ``workers=1``. Running SQLite under more than
one worker corrupts the single-writer assumption, so it must fail *before* the
server binds. Multi-worker PostgreSQL is allowed but is not a complete
distributed mode: it still needs sticky sessions and does not make the
in-process event bus / WebSocket fan-out shared. Those caveats are surfaced as
warnings so no launcher silently promotes an unsafe or half-configured topology.
"""

from __future__ import annotations

from dataclasses import dataclass


class DeploymentTopologyError(RuntimeError):
    """The requested process/database combination is unsafe."""


@dataclass(frozen=True, slots=True)
class DeploymentTopology:
    database_backend: str
    workers: int
    safe: bool
    warnings: tuple[str, ...] = ()


def validate_deployment_topology(
    *,
    database_backend: str,
    workers: int,
) -> DeploymentTopology:
    """Validate a (database backend, worker count) combination.

    Args:
        database_backend: Normalized backend, e.g. ``sqlite`` / ``postgresql``.
        workers: Number of server worker processes.

    Returns:
        A :class:`DeploymentTopology` describing the validated combination and
        any non-fatal boundary warnings.

    Raises:
        DeploymentTopologyError: If ``workers < 1`` or SQLite is asked to run
            under more than one worker.
    """
    if workers < 1:
        raise DeploymentTopologyError("workers must be >= 1")
    backend = (database_backend or "other").lower()
    if backend == "sqlite" and workers > 1:
        raise DeploymentTopologyError(
            "SQLite requires workers=1; switch to PostgreSQL before scaling processes"
        )
    warnings: tuple[str, ...] = ()
    if workers > 1:
        warnings = (
            "Multi-worker mode still requires sticky sessions for chat/WebSocket affinity.",
            "The in-process event bus is not made shared by RedisRunStore.",
        )
    return DeploymentTopology(
        database_backend=backend,
        workers=workers,
        safe=True,
        warnings=warnings,
    )
