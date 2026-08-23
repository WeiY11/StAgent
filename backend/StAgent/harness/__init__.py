"""Agent Harness — outer task lifecycle control system.

The harness wraps agent execution in a structured Task Episode lifecycle,
providing:

- **TaskEpisode**: one goal with one acceptance boundary
- **FindingsTracker**: structured issue/finding capture during execution
- **OutcomeEvaluator**: acceptance-criteria-based outcome evaluation
- **RepairPlanner**: corrective action planning on failure
- **SessionScorer**: session-level quality scoring
- **DurableReport**: persistent report generation
- **ValidationClosure**: pre/post-edit validation guards
- **HarnessHook**: integration into the AgentHook lifecycle
"""

from StAgent.harness.evaluator import OutcomeEvaluator
from StAgent.harness.findings import FindingsTracker
from StAgent.harness.hook import HarnessHook
from StAgent.harness.models import (
    AcceptanceCriteria,
    EpisodeOutcome,
    EpisodeStatus,
    Finding,
    FindingSeverity,
    RepairAction,
    RepairPlan,
    SessionScore,
    TaskEpisode,
    ValidationResult,
)
from StAgent.harness.repair import RepairPlanner
from StAgent.harness.report import DurableReport
from StAgent.harness.scorer import SessionScorer
from StAgent.harness.validation import ValidationClosure

__all__ = [
    # Models
    "AcceptanceCriteria",
    "EpisodeOutcome",
    "EpisodeStatus",
    "Finding",
    "FindingSeverity",
    "RepairAction",
    "RepairPlan",
    "SessionScore",
    "TaskEpisode",
    "ValidationResult",
    # Components
    "DurableReport",
    "FindingsTracker",
    "HarnessHook",
    "OutcomeEvaluator",
    "RepairPlanner",
    "SessionScorer",
    "ValidationClosure",
]
