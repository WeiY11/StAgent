"""Mission mode — durable goal-scoped autonomous execution.

Persistence (models + repository) lives in :mod:`leagent.db` per the
canonical layering; this package holds the runtime state machine
(:class:`MissionRunner`) and its wired seams:

- :class:`KernelSegmentExecutor` — one bounded ``AgentRuntime.stream`` /
  ``run_loop`` invocation per step (real model execution).
- :class:`LLMMissionPlanner` — plan generation + replan via the shared
  planner prompt templates.
- :class:`HeuristicVerifier` — verification gate built on the migrated
  :func:`assess_verification_gap` toolchain heuristics.

Still left for the follow-up layers: the ``/api/v1/missions`` routes, the
``MISSION_*`` event fan-out, and the missions frontend page.
"""

from leagent.mission.planner import LLMMissionPlanner
from leagent.mission.runner import (
    AlwaysPassVerifier,
    EventSink,
    IdentityContinuer,
    MissionContinuer,
    MissionPlanner,
    MissionRunner,
    MissionVerifier,
    SegmentExecutor,
    StaticPlanner,
    StepOutcome,
)
from leagent.mission.segment import KernelSegmentExecutor
from leagent.mission.types import (
    CONTINUABLE_SEGMENT_REASONS,
    MissionBudget,
    MissionPlan,
    MissionStepState,
    MissionUsage,
    SegmentResult,
    VerificationResult,
    parse_depends,
)
from leagent.mission.verifier import HeuristicVerifier, assess_verification_gap

__all__ = [
    "AlwaysPassVerifier",
    "CONTINUABLE_SEGMENT_REASONS",
    "EventSink",
    "HeuristicVerifier",
    "IdentityContinuer",
    "KernelSegmentExecutor",
    "LLMMissionPlanner",
    "MissionBudget",
    "MissionContinuer",
    "MissionPlan",
    "MissionPlanner",
    "MissionRunner",
    "MissionStepState",
    "MissionUsage",
    "MissionVerifier",
    "SegmentExecutor",
    "SegmentResult",
    "StaticPlanner",
    "StepOutcome",
    "VerificationResult",
    "assess_verification_gap",
    "parse_depends",
]
