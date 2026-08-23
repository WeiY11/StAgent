"""Agent core package.

This module provides the core agent functionality for StAgent:

* Base types for state, configuration, and execution context.
* :class:`AgentController` — the orchestration front door (workflow
  matching, hooks, permissions) that delegates to :class:`QueryEngine`
  by default.
* :class:`QueryEngine` — the session-scoped owner of the agentic turn
  loop (ported from the reference ``QueryEngine.ts``).
* :class:`TaskPlanner` — plan decomposition for the Plan-Execute path.
* :class:`ToolExecutor` — unified tool dispatcher (re-exported from the
  tools layer).
* :class:`ResultProcessor` / :class:`ErrorRecovery` — tool-result
  normalisation and retry middleware.
* :func:`fork_subagent` / :class:`AgentTool` — sub-agent delegation
  built on :meth:`QueryEngine.fork`.
* Lifecycle hooks and the ``ScriptAgentTool`` factory.

Example:
    from StAgent.agent import AgentController, AgentConfig, TaskPlanner
    from StAgent.agent import QueryEngine, fork_subagent
"""

from StAgent.agent.base import (
    AgentConfig,
    AgentContext,
    AgentMode,
    AgentResponse,
    AgentState,
    ConversationContext,
    ConversationMessage,
    ExecutionPlan,
    ExecutionStep,
    NoOpStreamHandler,
    PlanStep,
    QueuedStreamHandler,
    StepType,
    StreamEvent,
    StreamHandler,
    ToolCall,
    ToolResult,
)
from StAgent.agent.controller import AgentController
from StAgent.agent.hooks import (
    AgentHook,
    HookManager,
    LoggingHook,
    MetricsHook,
    RateLimitError,
    RateLimitHook,
    TaskHistoryHook,
    create_default_hooks,
)
from StAgent.agent.planner import (
    TaskPlanner,
    build_dependency_graph,
    get_parallel_groups,
    schedule_ready,
    topological_sort,
)
from StAgent.agent.query_engine import QueryEngine, QueryEngineConfig, SDKMessage
from StAgent.agent.recovery import ErrorRecovery, RecoveryHandler, ResultProcessor
from StAgent.agent.subagent import AgentTool, fork_subagent
from StAgent.agent.tool_use_context import ToolUseContext
from StAgent.agent.transitions import (
    Continue,
    ContinueReason,
    Terminal,
    TerminalReason,
)
from StAgent.tools.executor import ToolExecutor

__all__ = [
    # Base types
    "AgentState",
    "AgentMode",
    "StepType",
    "ToolCall",
    "ToolResult",
    "ExecutionStep",
    "AgentResponse",
    "StreamEvent",
    "StreamHandler",
    "NoOpStreamHandler",
    "QueuedStreamHandler",
    "AgentConfig",
    "AgentContext",
    "PlanStep",
    "ExecutionPlan",
    "ConversationMessage",
    "ConversationContext",
    # Controller
    "AgentController",
    # QueryEngine
    "QueryEngine",
    "QueryEngineConfig",
    "SDKMessage",
    "ToolUseContext",
    "Terminal",
    "TerminalReason",
    "Continue",
    "ContinueReason",
    # Planner
    "TaskPlanner",
    "build_dependency_graph",
    "topological_sort",
    "get_parallel_groups",
    "schedule_ready",
    # Executor / recovery
    "ToolExecutor",
    "ResultProcessor",
    "ErrorRecovery",
    "RecoveryHandler",
    # Sub-agents
    "fork_subagent",
    "AgentTool",
    # Hooks
    "AgentHook",
    "HookManager",
    "LoggingHook",
    "MetricsHook",
    "TaskHistoryHook",
    "RateLimitHook",
    "RateLimitError",
    "create_default_hooks",
]
