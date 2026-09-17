"""MCP (Model Context Protocol) client support for Antigona.

Thin, async wrapper around the official ``mcp`` Python SDK so the Antigona
brain can connect to external MCP servers (stdio, SSE, or streamable HTTP)
and expose their tools to the LLM — e.g. Gmail, Google Calendar, finance, etc.

Design goals:
- Composes with the single AntigonaBrain (no second runtime; this is a tool
  source feeding the existing core).
- Graceful fallback: if the ``mcp`` package is not installed or a server is
  unreachable, methods degrade to empty results / False — never crash import.

Usage::

    from antigona.core.mcp import MCPClient

    async with MCPClient.connect_stdio("npx", ["-y", "@some/mcp-server"]) as c:
        tools = await c.list_tools()
        result = await c.call_tool(tools[0].name, {"arg": 1})

    # or SSE / HTTP:
    async with MCPClient.connect_http("https://.../mcp/sse") as c: ...

``MCPRegistry`` is the persisted config (``~/.antigona/mcp_servers.json``) of
*which* servers exist; ``connect_from_entry()`` is the bridge that turns one
registry entry into a live ``MCPClient`` (resolving any API key from the env
or the Vault along the way)::

    from antigona.core.mcp import MCPRegistry, connect_from_entry

    reg = MCPRegistry.load()  # seeds built-in defaults (Context7) on first run
    async with await connect_from_entry(reg.servers["context7"]) as c:
        tools = await c.list_tools()

The ``mcp`` tool (``antigona.tools.registry._handle_mcp``) exposes this to
the LLM via function-calling: ``action="list"`` (registered servers),
``"add"``/``"remove"`` (persisted to disk), ``"tools"`` (connect + list a
server's tools), and ``"call"`` (connect + invoke one tool).
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from antigona.core import paths

logger = logging.getLogger(__name__)

__all__ = [
    "MCPClient",
    "MCPRegistry",
    "MCPTool",
    "connect_from_entry",
    "mcp_available",
]


def mcp_available() -> bool:
    """True when the ``mcp`` SDK is importable (else features degrade)."""
    try:
        import mcp  # noqa: F401

        return True
    except Exception:
        return False


@dataclass
class MCPTool:
    """A tool exposed by an MCP server, shaped for LLM function-calling."""

    name: str
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_types_tool(cls, tool: Any) -> MCPTool:
        return cls(
            name=getattr(tool, "name", ""),
            description=getattr(tool, "description", "") or "",
            input_schema=getattr(tool, "inputSchema", None) or {},
        )


class MCPClient:
    """Async MCP client over stdio/SSE/HTTP transports.

    Not thread-safe; intended to be used as a per-task or per-turn connection.
    """

    def __init__(self, session: Any, exit_stack: Any) -> None:
        self._session = session
        self._exit_stack = exit_stack
        self._initialized = False

    @classmethod
    async def connect_stdio(
        cls, command: str, args: list[str] | None = None, env: dict[str, str] | None = None
    ) -> MCPClient:
        """Connect to a stdio-based MCP server (e.g. ``npx -y <server>``)."""
        if not mcp_available():
            raise RuntimeError("mcp package not installed")
        from contextlib import AsyncExitStack

        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        stack = AsyncExitStack()
        params = StdioServerParameters(command=command, args=list(args or []), env=env)
        read, write = await stack.enter_async_context(stdio_client(params))
        session = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        client = cls(session, stack)
        client._initialized = True
        return client

    @classmethod
    async def connect_http(cls, url: str, headers: dict[str, str] | None = None) -> MCPClient:
        """Connect to an SSE or streamable-HTTP MCP server endpoint."""
        if not mcp_available():
            raise RuntimeError("mcp package not installed")
        from contextlib import AsyncExitStack

        from mcp import ClientSession
        from mcp.client.sse import sse_client
        from mcp.client.streamable_http import streamable_http_client

        stack = AsyncExitStack()
        # Pick the transport by endpoint shape, avoiding a double-attempt that
        # triggers an anyio cancel-scope bug in some mcp SDK versions (py3.13).
        if url.rstrip("/").endswith("/sse"):
            read, write = await stack.enter_async_context(sse_client(url, headers=headers))
        else:
            transport = await stack.enter_async_context(streamable_http_client(url))
            read, write = transport[:2]
        session = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        client = cls(session, stack)
        client._initialized = True
        return client

    async def list_tools(self) -> list[MCPTool]:
        if not self._initialized:
            return []
        try:
            result = await self._session.list_tools()
            return [MCPTool.from_types_tool(t) for t in result.tools]
        except Exception as exc:
            logger.warning("MCP list_tools failed: %s", exc)
            return []

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        if not self._initialized:
            return None
        try:
            result = await self._session.call_tool(name, arguments=arguments or {})
            # result is a CallToolResult with .content (list of blocks)
            content = getattr(result, "content", None)
            if isinstance(content, list):
                parts: list[str] = []
                for block in content:
                    if hasattr(block, "text"):
                        parts.append(str(block.text))
                    elif isinstance(block, dict):
                        parts.append(str(block.get("text", block)))
                return "\n".join(parts)
            return str(result)
        except Exception as exc:
            logger.warning("MCP call_tool %s failed: %s", name, exc)
            return None

    async def aclose(self) -> None:
        if self._exit_stack is not None:
            try:
                await self._exit_stack.aclose()
            except Exception:
                pass

    async def __aenter__(self) -> MCPClient:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

# Default on-disk location for the persisted registry. Overridable per-call
# (tests pass an explicit ``path``) but this is what every real caller (the
# ``mcp`` tool handler) uses so state survives across tool calls / processes.
# Resolved lazily through the governed runtime root so importing this module
# never fails closed and never touches the read-only code root.  ``None`` here
# means "use the governed default"; tests override this attribute directly.
_REGISTRY_PATH: Path | None = None


def _registry_path() -> Path:
    return _REGISTRY_PATH if _REGISTRY_PATH is not None else paths.mcp_registry_file()


def _default_servers() -> dict[str, dict[str, Any]]:
    """Servers available out of the box, before any user ``mcp add``.

    Context7 (https://context7.com) is Upstash's up-to-date library
    documentation server — useful for code generation/review so the model
    isn't relying on stale training data. Works with no API key (lower rate
    limits); set ``CONTEXT7_API_KEY`` (env var or Vault) for higher limits —
    it is injected at connect time via :func:`connect_from_entry`, never
    written to the registry file on disk.
    """
    return {
        "context7": {
            "kind": "stdio",
            "command": "npx",
            "args": ["-y", "@upstash/context7-mcp"],
            "api_key_env": "CONTEXT7_API_KEY",
        },
    }


@dataclass
class MCPRegistry:
    """Registry of configured MCP servers (name -> spec).

    File/JSON backed (``~/.antigona/mcp_servers.json`` by default) so the
    brain can load MCP tool sources from config without importing heavy SDKs
    at startup. The bare constructor builds an empty in-memory registry;
    use :meth:`load` to read persisted state (seeding built-in defaults such
    as Context7 on first run) and :meth:`save` to persist changes.
    """

    servers: dict[str, dict[str, Any]] = field(default_factory=dict)

    def add_stdio(
        self,
        name: str,
        command: str,
        args: list[str] | None = None,
        *,
        api_key_env: str | None = None,
    ) -> None:
        entry: dict[str, Any] = {"kind": "stdio", "command": command, "args": list(args or [])}
        if api_key_env:
            entry["api_key_env"] = api_key_env
        self.servers[name] = entry

    def add_http(
        self,
        name: str,
        url: str,
        headers: dict[str, str] | None = None,
        *,
        api_key_env: str | None = None,
    ) -> None:
        entry: dict[str, Any] = {"kind": "http", "url": url, "headers": headers or {}}
        if api_key_env:
            entry["api_key_env"] = api_key_env
        self.servers[name] = entry

    def remove(self, name: str) -> bool:
        return self.servers.pop(name, None) is not None

    def names(self) -> list[str]:
        return sorted(self.servers.keys())

    def to_dict(self) -> dict[str, Any]:
        return self.servers

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MCPRegistry:
        return cls(servers=dict(data or {}))

    # ── Persistence ──────────────────────────────────────────────────────

    def save(self, path: str | Path | None = None) -> None:
        """Persist the registry to *path* (default ``~/.antigona/mcp_servers.json``).

        Writes atomically (tmp file in the same directory + ``os.replace``)
        so a concurrent reader never observes a half-written file — the same
        minimal-locking approach ``secrets/vault.py`` uses.
        """
        p = Path(path) if path is not None else _registry_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(f"{p.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(self.servers, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, p)
        try:
            p.chmod(0o600)
        except OSError:
            pass

    @classmethod
    def load(cls, path: str | Path | None = None) -> MCPRegistry:
        """Load the registry from *path*, seeding built-in defaults on first run.

        - No file yet: returns a registry pre-populated with the built-in
          servers (currently Context7) — nothing is written to disk until
          :meth:`save` is called.
        - Corrupt/unreadable file: logs a warning and falls back to an empty
          registry (never raises — a bad config file must not break the
          ``mcp`` tool call).
        """
        p = Path(path) if path is not None else _registry_path()
        if not p.exists():
            return cls(servers=_default_servers())
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("MCP registry file must contain a JSON object")
            return cls.from_dict(data)
        except (json.JSONDecodeError, OSError, ValueError) as exc:
            logger.warning("Failed to load MCP registry from %s: %s", p, exc)
            return cls()


def _resolve_secret(env_name: str) -> str | None:
    """Resolve a named secret: explicit env var first, then the Vault.

    Mirrors the precedence used in ``providers/profiles.py``
    (``ProfileRegistry.resolve``): explicit > env > vault.
    """
    val = os.environ.get(env_name, "").strip()
    if val:
        return val
    try:
        from antigona.secrets.vault import Vault

        return Vault().get(env_name)
    except Exception:
        return None


async def connect_from_entry(entry: dict[str, Any]) -> MCPClient:
    """Build a live :class:`MCPClient` from a registry entry dict.

    This is the bridge between config (:class:`MCPRegistry`, static JSON)
    and an actual connection: given one ``MCPRegistry.servers[name]`` entry,
    it resolves any configured API key (env var or Vault, by
    ``entry["api_key_env"]``) and connects — stdio servers get the secret
    appended as a CLI arg (``api_key_arg``, default ``--api-key``); http
    servers get it as a header (``api_key_header``, default the env var
    name). The secret is never written back into the entry / registry file.
    """
    kind = entry.get("kind", "stdio")
    secret: str | None = None
    env_name = entry.get("api_key_env")
    if env_name:
        secret = _resolve_secret(env_name)

    if kind == "http":
        url = entry.get("url", "")
        headers = dict(entry.get("headers") or {})
        if secret:
            header_name = entry.get("api_key_header") or env_name or "Authorization"
            headers.setdefault(header_name, secret)
        return await MCPClient.connect_http(url, headers=headers or None)

    command = entry.get("command", "")
    args = list(entry.get("args") or [])
    if secret:
        arg_flag = entry.get("api_key_arg", "--api-key")
        args = [*args, arg_flag, secret]
    return await MCPClient.connect_stdio(command, args)
