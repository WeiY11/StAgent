"""Concrete :class:`TaskHandler` implementations for each :class:`TaskType`.

Each module in this package exports exactly one handler class; they are
registered with the :class:`TaskManager` during startup via
:mod:`StAgent.tasks.registration`.
"""

from __future__ import annotations

from StAgent.tasks.handlers.agent_handler import AgentTaskHandler
from StAgent.tasks.handlers.batch_handler import BatchTaskHandler
from StAgent.tasks.handlers.shell_handler import ShellTaskHandler
from StAgent.tasks.handlers.tool_handler import ToolTaskHandler
from StAgent.tasks.handlers.workflow_handler import WorkflowTaskHandler

__all__ = [
    "AgentTaskHandler",
    "BatchTaskHandler",
    "ShellTaskHandler",
    "ToolTaskHandler",
    "WorkflowTaskHandler",
]
