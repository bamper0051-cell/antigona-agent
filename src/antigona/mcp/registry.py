"""MCP Registry — manage connected MCP servers."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from antigona.mcp.client import MCPClient, SSEMCPClient, StdioMCPClient
from antigona.mcp.discovery import MCPToolInfo, ToolDiscovery
from antigona.mcp.execution import ToolExecution

logger = logging.getLogger(__name__)


class MCPRegistry:
    """Registry for managing MCP server connections.

    Provides methods to register, connect, list, and disconnect MCP servers.
    """

    def __init__(self) -> None:
        self._clients: dict[str, MCPClient] = {}
        self._discoveries: dict[str, ToolDiscovery] = {}
        self._executors: dict[str, ToolExecution] = {}

    def register_stdio(
        self,
        name: str,
        command: str,
        args: list[str] | None = None,
    ) -> None:
        """Register a stdio-based MCP server.

        Args:
            name: Unique server name.
            command: Executable command.
            args: Command-line arguments.
        """
        client = StdioMCPClient(name=name, command=command, args=args)
        self._clients[name] = client
        self._discoveries[name] = ToolDiscovery(client)
        self._executors[name] = ToolExecution(client)
        logger.info("Registered stdio MCP server '%s' (%s)", name, command)

    def register_sse(
        self,
        name: str,
        server_url: str,
        headers: dict[str, str] | None = None,
    ) -> None:
        """Register an SSE (HTTP)-based MCP server.

        Args:
            name: Unique server name.
            server_url: Base URL of the MCP server.
            headers: Optional HTTP headers.
        """
        client = SSEMCPClient(name=name, server_url=server_url, headers=headers)
        self._clients[name] = client
        self._discoveries[name] = ToolDiscovery(client)
        self._executors[name] = ToolExecution(client)
        logger.info("Registered SSE MCP server '%s' (%s)", name, server_url)

    async def connect(self, name: str) -> bool:
        """Connect to a registered MCP server.

        Args:
            name: Server name.

        Returns:
            True if connected, False on failure.
        """
        client = self._clients.get(name)
        if client is None:
            logger.error("MCP server '%s' not registered", name)
            return False
        return await client.connect()

    async def disconnect(self, name: str) -> bool:
        """Disconnect a registered MCP server.

        Args:
            name: Server name.

        Returns:
            True if disconnected, False if not found.
        """
        client = self._clients.get(name)
        if client is None:
            return False
        await client.close()
        return True

    async def list_tools(self, name: str) -> list[MCPToolInfo]:
        """List tools from a connected MCP server.

        Args:
            name: Server name.

        Returns:
            List of available tools.
        """
        discovery = self._discoveries.get(name)
        if discovery is None:
            logger.error("MCP server '%s' not registered", name)
            return []
        return await discovery.list_tools()

    async def call_tool(
        self,
        server_name: str,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
    ) -> Any:
        """Call a tool on a connected MCP server.

        Args:
            server_name: MCP server name.
            tool_name: Tool name to call.
            arguments: Tool arguments.

        Returns:
            Tool execution result.
        """
        executor = self._executors.get(server_name)
        if executor is None:
            raise ValueError(f"MCP server '{server_name}' not registered")
        return await executor.call_tool(tool_name, arguments)

    def list_servers(self) -> list[dict[str, Any]]:
        """List all registered servers with connection status."""
        result: list[dict[str, Any]] = []
        for name, client in self._clients.items():
            result.append(
                {
                    "name": name,
                    "type": "stdio" if isinstance(client, StdioMCPClient) else "sse",
                    "connected": client.connected,
                }
            )
        return result

    def unregister(self, name: str) -> bool:
        """Unregister an MCP server (disconnects first).

        Args:
            name: Server name.

        Returns:
            True if unregistered, False if not found.
        """
        if name not in self._clients:
            return False

        client = self._clients[name]
        # Best-effort disconnect: if there's a running loop, schedule it
        try:
            loop = asyncio.get_running_loop()
            if client.connected:
                loop.create_task(client.close())
        except RuntimeError:
            pass  # no running event loop

        del self._clients[name]
        self._discoveries.pop(name, None)
        self._executors.pop(name, None)
        return True

    @property
    def server_count(self) -> int:
        return len(self._clients)

    def get_client(self, name: str) -> MCPClient | None:
        """Get a registered client by name."""
        return self._clients.get(name)
