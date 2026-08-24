"""Workflow execution observability plane.

A strictly **read-only** observation layer over the existing progress-event
stream. Nothing in this package may influence scheduling, node execution, or
cache/retry/loop semantics — it only listens, accumulates and serves.

Three pieces:

* :mod:`~StAgent.workflow.observability.gantt` — turn an event stream (or a
  persisted ``execution_history``) into a lane-assigned Gantt chart.
* :mod:`~StAgent.workflow.observability.profile` — derive bottleneck / critical
  path / parallelism statistics from a Gantt chart.
* :mod:`~StAgent.workflow.observability.hub` — process-level registry of live
  executions plus a bounded cache of finished Gantt charts.
"""

from .gantt import ExecutionGantt, GanttBar, GanttBuilder
from .hub import LiveExecution, ObservabilityHub, get_observability_hub
from .profile import ExecutionProfile, NodeShare, build_profile

__all__ = [
    "ExecutionGantt",
    "ExecutionProfile",
    "GanttBar",
    "GanttBuilder",
    "LiveExecution",
    "NodeShare",
    "ObservabilityHub",
    "build_profile",
    "get_observability_hub",
]
