"""Tool discovery — list available tools from an MCP server."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from antigona.mcp.client import MCPClient, MCPResponse

logger = logging.getLogger(__name__)


@dataclass
class MCPToolInfo:
    """Information about a tool exposed by an MCP server."""

    name: str
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)


class ToolDiscovery:
    """Discovers tools available on an MCP server."""

    def __init__(self, client: MCPClient) -> None:
        self._client = client

    async def list_tools(self) -> list[MCPToolInfo]:
        """Request the list of tools from the connected MCP server.

        Uses the ``tools/list`` JSON-RPC method.

        Returns:
            List of MCPToolInfo. Empty list on error.
        """
        if not self._client.connected:
            logger.warning("MCP client '%s' is not connected", self._client.name)
            return []

        response: MCPResponse = await self._client.send_request("tools/list")
        if not response.success:
            logger.error(
                "Failed to list tools on '%s': %s",
                self._client.name,
                response.error,
            )
            return []

        raw_tools = response.result
        if not isinstance(raw_tools, list):
            return []

        tools: list[MCPToolInfo] = []
        for raw in raw_tools:
            if isinstance(raw, dict):
                tools.append(
                    MCPToolInfo(
                        name=raw.get("name", "unknown"),
                        description=raw.get("description", ""),
                        input_schema=raw.get("inputSchema", {}),
                    )
                )
        return tools
