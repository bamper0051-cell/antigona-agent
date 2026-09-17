"""ACP (Agent Communication Protocol) client support for Antigona.

Thin wrapper around the official ``acp-sdk`` (IBM) so the Antigona brain can
drive EXTERNAL ACP-compatible agents (e.g. Codex, Claude, any ACP server) and
expose their capabilities to the user — the same spirit as MCP but for agent
sessions rather than tools.

Design goals:
- Composes with the single AntigonaBrain (no second runtime; this is an
  external-agent driver feeding the existing core).
- Graceful fallback: if ``acp_sdk`` is absent or a server is unreachable,
  methods degrade to empty/False — never crash import or the main loop.

Usage::

    from antigona.core.acp import ACPClient, ACPRegistry

    reg = ACPRegistry()
    reg.add("codex", "http://127.0.0.1:8000")

    async with ACPClient(base_url="http://127.0.0.1:8000") as c:
        await c.ping()
        reply = await c.run_sync(prompt="summarize this repo")
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

__all__ = ["ACPClient", "ACPRegistry", "acp_available"]


def acp_available() -> bool:
    """True when the ``acp_sdk`` is importable (else features degrade)."""
    try:
        import acp_sdk  # noqa: F401

        return True
    except Exception:
        return False


@dataclass
class ACPRegistry:
    """Registry of configured ACP servers (name -> base_url)."""

    agents: dict[str, str] = field(default_factory=dict)

    def add(self, name: str, base_url: str) -> None:
        self.agents[name] = base_url

    def remove(self, name: str) -> bool:
        return self.agents.pop(name, None) is not None

    def names(self) -> list[str]:
        return sorted(self.agents.keys())

    def to_dict(self) -> dict[str, str]:
        return dict(self.agents)

    @classmethod
    def from_dict(cls, data: dict[str, str]) -> ACPRegistry:
        return cls(agents=dict(data or {}))


class ACPClient:
    """Async ACP client over an external ACP server."""

    def __init__(self, base_url: str, timeout: float = 60.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._client: Any = None
        self._session: Any = None

    @property
    def _sdk(self) -> Any:
        if not acp_available():
            raise RuntimeError("acp_sdk not installed")
        from acp_sdk.client import Client

        return Client

    async def connect(self) -> ACPClient:
        """Create the SDK client + session. Call once before other methods."""
        from acp_sdk.client import Client

        self._client = Client(base_url=self.base_url, timeout=self.timeout)
        self._session = await self._client.session.create(name="antigona")
        return self

    async def ping(self) -> bool:
        if self._client is None:
            return False
        try:
            await self._client.ping()
            return True
        except Exception as exc:
            logger.warning("ACP ping failed for %s: %s", self.base_url, exc)
            return False

    async def agents(self) -> list[str]:
        if self._client is None:
            return []
        try:
            resp = await self._client.agents()
            return [a.name for a in (resp.agents or [])]
        except Exception as exc:
            logger.warning("ACP agents failed: %s", exc)
            return []

    async def run_sync(self, prompt: str, session_id: str = "") -> str:
        """Send a prompt to the agent and return the textual reply."""
        if self._client is None:
            return ""
        try:
            run = await self._client.run_sync(
                session_id=session_id or str(self._session.session_id),
                prompt=prompt,
            )
            parts = []
            for event in getattr(run, "events", []):
                for msg in getattr(event, "messages", []):
                    for part in getattr(msg, "message", []):
                        if hasattr(part, "text") and part.text:
                            parts.append(part.text)
            return "\n".join(parts) if parts else str(run)
        except Exception as exc:
            logger.warning("ACP run_sync failed: %s", exc)
            return ""

    async def run_status(self, session_id: str = "") -> str:
        if self._client is None:
            return ""
        try:
            resp = await self._client.run_status(
                session_id=session_id or str(self._session.session_id)
            )
            return str(getattr(resp, "status", ""))
        except Exception as exc:
            logger.warning("ACP run_status failed: %s", exc)
            return ""

    async def aclose(self) -> None:
        if self._client is not None:
            try:
                await self._client.close()
            except Exception:
                pass
            self._client = None

    async def __aenter__(self) -> ACPClient:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()
