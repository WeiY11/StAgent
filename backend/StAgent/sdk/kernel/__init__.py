"""Kernel sub-package — agent run loop internals.

The kernel owns the think-act-observe loop, run-state management,
checkpoint/resume, and the unified event emission pipeline. It is
not part of the stable public API — SDK consumers use
:class:`~StAgent.sdk.AgentRuntime` instead.
"""

from StAgent.sdk.kernel.checkpoint import InMemoryCheckpointStore, create_checkpoint
from StAgent.sdk.kernel.event_mapper import KernelEventMapper
from StAgent.sdk.kernel.loop import RESUMABLE_CHECKPOINT_REASONS, run_loop, run_to_result
from StAgent.sdk.kernel.session_registry import AgentRunTaskRecord, SessionTaskRegistry
from StAgent.sdk.kernel.state import RunState

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
