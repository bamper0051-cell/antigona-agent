"""Tool execution — call tools on an MCP server."""

from __future__ import annotations

import logging
from typing import Any

from antigona.mcp.client import MCPClient, MCPResponse

logger = logging.getLogger(__name__)


class MCPExecutionError(Exception):
    """Raised when a tool execution on an MCP server fails."""


class ToolExecution:
    """Executes tools on a connected MCP server."""

    def __init__(self, client: MCPClient) -> None:
        self._client = client

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
    ) -> Any:
        """Call a tool on the MCP server.

        Uses the ``tools/call`` JSON-RPC method.

        Args:
            name: Tool name to invoke.
            arguments: Optional arguments for the tool.

        Returns:
            The tool result.

        Raises:
            MCPExecutionError: on execution failure.
        """
        if not self._client.connected:
            raise MCPExecutionError(
                f"MCP client '{self._client.name}' is not connected"
            )

        params: dict[str, Any] = {"name": name}
        if arguments:
            params["arguments"] = arguments

        response: MCPResponse = await self._client.send_request("tools/call", params)
        if not response.success:
            raise MCPExecutionError(
                f"Tool '{name}' on '{self._client.name}' failed: {response.error}"
            )

        return response.result
