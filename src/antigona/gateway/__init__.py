"""Tool Gateway — per-tool backend selection for Antigona's tools.

Re-exports the primary gateway API:

- :class:`ToolConfig` — per-tool backend + use_gateway flag
- :class:`ToolGatewayConfig` — all-tools configuration with persistence
- :class:`GatewayRouter` — resolve tool → (config, use_gateway)
- :func:`get_gateway` — module-level singleton
"""

from __future__ import annotations

from .main import main  # noqa: F401  — entrypoint antigona-gateway (HTTP Gateway)
from .tool_gateway import (
    Backend,
    GatewayRouter,
    ToolConfig,
    ToolGatewayConfig,
    ToolName,
    get_gateway,
    handle_tool_gateway_command,
    reset_gateway,
)

__all__ = [
    "Backend",
    "GatewayRouter",
    "ToolConfig",
    "ToolGatewayConfig",
    "ToolName",
    "get_gateway",
    "handle_tool_gateway_command",
    "main",
    "reset_gateway",
]
