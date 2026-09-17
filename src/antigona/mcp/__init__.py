"""MCP (Model Context Protocol) adapter for Antigona.

Provides:
  - MCPClient: connect to MCP servers (stdio, HTTP SSE)
  - ToolDiscovery: list tools from an MCP server
  - ToolExecution: call tools on an MCP server
  - MCPRegistry: manage connected MCP servers
"""

from __future__ import annotations

from antigona.mcp.client import MCPClient, MCPConnectionError
from antigona.mcp.discovery import MCPToolInfo, ToolDiscovery
from antigona.mcp.execution import MCPExecutionError, ToolExecution
from antigona.mcp.registry import MCPRegistry

__all__ = [
    "MCPClient",
    "MCPConnectionError",
    "MCPExecutionError",
    "MCPRegistry",
    "MCPToolInfo",
    "ToolDiscovery",
    "ToolExecution",
]
