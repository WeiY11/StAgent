"""Kernel sub-package — agent run loop internals.

The kernel owns the think-act-observe loop, run-state management,
checkpoint/resume, and the unified event emission pipeline. It is
not part of the stable public API — SDK consumers use
:class:`~leagent.sdk.AgentRuntime` instead.
"""

from leagent.sdk.kernel.checkpoint import InMemoryCheckpointStore, create_checkpoint
from leagent.sdk.kernel.event_mapper import KernelEventMapper
from leagent.sdk.kernel.loop import RESUMABLE_CHECKPOINT_REASONS, run_loop, run_to_result
from leagent.sdk.kernel.session_registry import AgentRunTaskRecord, SessionTaskRegistry
from leagent.sdk.kernel.state import RunState

__all__ = [
    "AgentRunTaskRecord",
    "InMemoryCheckpointStore",
    "KernelEventMapper",
    "RESUMABLE_CHECKPOINT_REASONS",
    "RunState",
    "SessionTaskRegistry",
    "create_checkpoint",
    "run_loop",
    "run_to_result",
]
