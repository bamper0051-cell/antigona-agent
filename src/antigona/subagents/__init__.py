"""Subagent delegation — Protocol + CLI adapters.

SubagentAdapter provides a common async interface for delegating tasks
to external coding agents (Claude Code CLI, Codex CLI, etc.). The
SubagentRegistry manages available adapters and selects the right one
based on task type.
"""

from __future__ import annotations

from antigona.subagents.adapters.claude_code import ClaudeCodeAdapter
from antigona.subagents.adapters.codex import CodexAdapter
from antigona.subagents.base import ExecutionStatus, SubagentAdapter, SubagentResult, TaskType
from antigona.subagents.registry import AdapterNotFoundError, SubagentRegistry

__all__ = [
    "SubagentAdapter",
    "SubagentResult",
    "TaskType",
    "ExecutionStatus",
    "SubagentRegistry",
    "AdapterNotFoundError",
    "ClaudeCodeAdapter",
    "CodexAdapter",
]
