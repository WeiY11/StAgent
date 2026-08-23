"""Tools package — agent tool implementations.

This module provides the core tool framework for StAgent including:
- Base tool abstractions and result types
- Tool registry for discovery and management
- Tool executor for parallel and sequential execution

Example:
    >>> from StAgent.tools import BaseTool, ToolContext, ToolResult
    >>> from StAgent.tools import get_registry, get_executor
    >>>
    >>> # Register a custom tool
    >>> registry = get_registry()
    >>> registry.register(MyCustomTool())
    >>>
    >>> # Execute tools
    >>> executor = get_executor()
    >>> result = await executor.execute("my_tool", params, context)
"""

from StAgent.tools.base import (
    BaseTool,
    SyncTool,
    ToolCapability,
    ToolCategory,
    ToolContext,
    ToolProgressEvent,
    ToolResult,
)
from StAgent.tools.executor import (
    AggregatedResult,
    ExecutionResult,
    ToolCall,
    ToolExecutionError,
    ToolExecutor,
    get_executor,
    reset_executor,
)
from StAgent.tools.registry import (
    ToolNotFoundError,
    ToolRegistrationError,
    ToolRegistry,
    get_registry,
    reset_registry,
)

__all__ = [
    # Base classes and types
    "BaseTool",
    "SyncTool",
    "ToolCapability",
    "ToolCategory",
    "ToolContext",
    "ToolProgressEvent",
    "ToolResult",
    # Registry
    "ToolRegistry",
    "ToolNotFoundError",
    "ToolRegistrationError",
    "get_registry",
    "reset_registry",
    # Executor
    "ToolExecutor",
    "ToolCall",
    "ExecutionResult",
    "AggregatedResult",
    "ToolExecutionError",
    "get_executor",
    "reset_executor",
]
