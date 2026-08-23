"""MCP (Model Context Protocol) package.

This package provides client connectivity to MCP servers for tool,
prompt, and resource integration.
"""

from StAgent.mcp.base import (
    MCPCapabilities,
    MCPPrompt,
    MCPResource,
    MCPServer,
    MCPServerInfo,
    MCPTool,
    MCPTransport,
)
from StAgent.mcp.client import (
    MCPClient,
    MCPConnectionError,
    MCPProtocolError,
)
from StAgent.mcp.manager import (
    ManagedClient,
    MCPClientManager,
    ServerHealth,
    get_mcp_manager,
    reset_mcp_manager,
)

__all__ = [
    "MCPCapabilities",
    "MCPClient",
    "MCPClientManager",
    "MCPConnectionError",
    "MCPPrompt",
    "MCPProtocolError",
    "MCPResource",
    "MCPServer",
    "MCPServerInfo",
    "MCPTool",
    "MCPTransport",
    "ManagedClient",
    "ServerHealth",
    "get_mcp_manager",
    "reset_mcp_manager",
]
