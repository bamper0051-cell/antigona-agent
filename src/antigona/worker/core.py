"""Worker core — SubagentRegistry setup and adapter wiring.

Creates and configures the SubagentRegistry with available CLI adapters,
supporting fallback: if Claude Code is unavailable, Codex is tried next.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from antigona.subagents import SubagentRegistry
from antigona.subagents.base import TaskType

if TYPE_CHECKING:
    from antigona.subagents.base import SubagentAdapter

LOGGER = logging.getLogger("antigona.worker.core")


def build_subagent_registry(
    claude_adapter: SubagentAdapter | None = None,
    codex_adapter: SubagentAdapter | None = None,
) -> SubagentRegistry:
    """Build and populate a SubagentRegistry with available CLI adapters.

    Each adapter is only registered if its CLI binary is on $PATH.
    """
    from antigona.subagents.adapters.claude_code import ClaudeCodeAdapter
    from antigona.subagents.adapters.codex import CodexAdapter

    registry = SubagentRegistry()

    actual_claude = claude_adapter or ClaudeCodeAdapter()
    if getattr(actual_claude, "available", lambda: True)():
        registry.register("claude", actual_claude)
        LOGGER.info("SubagentRegistry: registered claude adapter")
    else:
        LOGGER.info("SubagentRegistry: claude CLI not available — skipping")

    actual_codex = codex_adapter or CodexAdapter()
    if getattr(actual_codex, "available", lambda: True)():
        registry.register("codex", actual_codex)
        LOGGER.info("SubagentRegistry: registered codex adapter")
    else:
        LOGGER.info("SubagentRegistry: codex CLI not available — skipping")

    if not registry.available:
        LOGGER.warning(
            "SubagentRegistry: no CLI adapters available — "
            "subagent delegation will not work"
        )

    return registry


def select_adapter_for_task(
    registry: SubagentRegistry,
    task_type: TaskType,
) -> SubagentAdapter | None:
    """Select the best adapter for *task_type*, with fallback.

    If the primary adapter for the task type fails (e.g. binary not found),
    the secondary is returned instead. Returns None when no adapter is
    registered at all.
    """
    try:
        candidates = registry.select(task_type)
        return candidates[0]
    except LookupError:
        LOGGER.warning("No adapter found for task type %s", task_type)
        return None
