"""Adapters package — concrete SubagentAdapter implementations."""

from __future__ import annotations

from antigona.subagents.adapters.claude_code import ClaudeCodeAdapter
from antigona.subagents.adapters.codex import CodexAdapter

__all__ = [
    "ClaudeCodeAdapter",
    "CodexAdapter",
]
