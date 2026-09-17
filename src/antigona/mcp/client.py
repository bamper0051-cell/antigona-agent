"""MCP client — connects to MCP servers over stdio or HTTP SSE."""

from __future__ import annotations

import asyncio
import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


class MCPConnectionError(Exception):
    """Raised when connection to an MCP server fails."""


@dataclass
class MCPResponse:
    """Standard response from an MCP server."""

    success: bool = True
    result: Any = None
    error: str | None = None
    tool_name: str = ""


class MCPClient(ABC):
    """Abstract base for MCP server connections."""

    def __init__(self, name: str, server_url: str | None = None):
        self.name = name
        self.server_url = server_url
        self._connected: bool = False

    @property
    def connected(self) -> bool:
        return self._connected

    @abstractmethod
    async def connect(self) -> bool:
        """Establish connection to the MCP server.

        Returns:
            True if connected, False on failure.
        """

    @abstractmethod
    async def send_request(self, method: str, params: dict[str, Any] | None = None) -> MCPResponse:
        """Send a JSON-RPC request to the MCP server.

        Args:
            method: The JSON-RPC method name.
            params: Optional parameters.

        Returns:
            MCPResponse with result or error.
        """

    @abstractmethod
    async def close(self) -> None:
        """Close the connection."""


class StdioMCPClient(MCPClient):
    """MCP client connecting via subprocess stdio.

    Launches a process and communicates via stdin/stdout JSON-RPC.
    """

    def __init__(self, name: str, command: str, args: list[str] | None = None) -> None:
        super().__init__(name, server_url=None)
        self._command = command
        self._args = args or []
        self._process: asyncio.subprocess.Process | None = None
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    async def connect(self) -> bool:
        """Launch the subprocess and open stdio channels."""
        try:
            self._process = await asyncio.create_subprocess_exec(
                self._command,
                *self._args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            # We'll read/write via the raw pipe.
            # For simplicity, accumulate output from stdout pipe.
            self._connected = True
            logger.info(
                "Connected to stdio MCP server '%s' via %s",
                self.name, self._command,
            )
            return True
        except (FileNotFoundError, PermissionError) as exc:
            logger.error("Failed to start MCP server '%s': %s", self.name, exc)
            self._connected = False
            return False

    async def send_request(self, method: str, params: dict[str, Any] | None = None) -> MCPResponse:
        """Write JSON-RPC request to stdin, read one JSON line from stdout."""
        if not self._connected or self._process is None:
            return MCPResponse(success=False, error="Not connected", tool_name="")

        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
        }
        if params:
            request["params"] = params

        request_str = json.dumps(request) + "\n"

        try:
            assert self._process.stdin is not None
            self._process.stdin.write(request_str.encode("utf-8"))
            await self._process.stdin.drain()

            assert self._process.stdout is not None
            response_line = await asyncio.wait_for(
                self._process.stdout.readline(), timeout=30
            )

            if not response_line:
                return MCPResponse(success=False, error="Empty response from server")

            response = json.loads(response_line.decode("utf-8"))

            if "error" in response and response["error"]:
                return MCPResponse(
                    success=False,
                    error=str(response["error"]),
                    tool_name=params.get("name", "") if params else "",
                )

            return MCPResponse(
                success=True,
                result=response.get("result"),
                tool_name=params.get("name", "") if params else "",
            )

        except TimeoutError:
            return MCPResponse(success=False, error="Request timed out")
        except (json.JSONDecodeError, OSError) as exc:
            return MCPResponse(success=False, error=str(exc))

    async def close(self) -> None:
        """Terminate the subprocess."""
        self._connected = False
        if self._process and self._process.returncode is None:
            try:
                self._process.terminate()
                await asyncio.wait_for(self._process.wait(), timeout=5)
            except (TimeoutError, ProcessLookupError):
                if self._process and self._process.returncode is None:
                    self._process.kill()


class SSEMCPClient(MCPClient):
    """MCP client connecting via HTTP SSE (Server-Sent Events)."""

    def __init__(
        self,
        name: str,
        server_url: str,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(name, server_url)
        self._headers = headers or {}
        self._session: Any = None  # httpx.AsyncClient

    async def connect(self) -> bool:
        """Open an HTTP connection for SSE-based communication."""
        try:
            import httpx

            base_url: str = self.server_url or ""
            self._session = httpx.AsyncClient(
                base_url=base_url,
                headers=self._headers,
                timeout=30,
            )
            # Probe the endpoint
            resp = await self._session.get("/health")
            if resp.status_code < 500:
                self._connected = True
                logger.info(
                    "Connected to SSE MCP server '%s' at %s",
                    self.name, self.server_url,
                )
                return True
            else:
                logger.error(
                    "SSE MCP server '%s' returned %d",
                    self.name, resp.status_code,
                )
                self._connected = False
                return False
        except ImportError:
            logger.error("httpx is required for SSE MCP client")
            self._connected = False
            return False
        except Exception as exc:
            logger.error("Failed to connect to SSE MCP '%s': %s", self.name, exc)
            self._connected = False
            return False

    async def send_request(self, method: str, params: dict[str, Any] | None = None) -> MCPResponse:
        """Send a POST request to the MCP endpoint."""
        if not self._connected or self._session is None:
            return MCPResponse(success=False, error="Not connected")

        try:
            payload: dict[str, Any] = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": method,
            }
            if params:
                payload["params"] = params

            resp = await self._session.post("/", json=payload)

            if resp.status_code >= 400:
                return MCPResponse(
                    success=False,
                    error=f"HTTP {resp.status_code}: {resp.text[:200]}",
                )

            data = resp.json()

            if "error" in data and data["error"]:
                return MCPResponse(
                    success=False,
                    error=str(data["error"]),
                )

            return MCPResponse(success=True, result=data.get("result"))

        except Exception as exc:
            return MCPResponse(success=False, error=str(exc))

    async def close(self) -> None:
        """Close the HTTP session."""
        self._connected = False
        if self._session:
            await self._session.aclose()
            self._session = None
