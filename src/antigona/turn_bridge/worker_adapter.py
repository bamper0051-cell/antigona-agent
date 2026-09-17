"""TurnWorker — bridge TaskFlow goals through the TurnEngine for read-only tasks.

The TurnWorker wraps a :class:`TurnEngine` and a set of workspace-scoped
read-only tools (read_file, check_file, list_dir) so that read-only flows
can be executed with the full model → tool → result → model cycle and
built-in error recovery.

Usage::

    worker = TurnWorker(
        base_url="https://api.openai.com/v1",
        api_key="sk-...",
        model="gpt-4o",
        workspace_path=str(paths.workspace_dir()),
    )
    result = await worker.execute_task(
        flow_id="abc-123",
        goal="Read the config file",
        messages=[{"role": "user", "content": "Read main.py"}],
        tools=READ_ONLY_TOOLS,
    )
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from antigona.core import paths
from antigona.turn_bridge.provider_adapter import (
    ProviderAdapter,
    ProviderAdapterConfig,
)
from antigona.turn_bridge.turn_engine_adapter import (
    TurnBudget,
    TurnEngine,
    TurnResult,
)

LOGGER = logging.getLogger(__name__)

# ── Workspace path helpers ──────────────────────────────────────────────────

DEFAULT_WORKSPACE = str(paths.project_root())


def _resolve_workspace(workspace_path: str | None = None) -> Path:
    """Resolve the workspace root, falling back to env/constant."""
    if workspace_path:
        root = Path(workspace_path).resolve()
    else:
        env = os.environ.get("ANTIGONA_WORKSPACE")
        root = Path(env).resolve() if env else Path(DEFAULT_WORKSPACE).resolve()
    return root


def _safe_workspace_path(requested: str, workspace: Path) -> Path:
    """Resolve *requested* relative to *workspace* and ensure it stays inside.

    Raises:
        ValueError: If the resolved path escapes the workspace boundary.
    """
    target = workspace.joinpath(requested).resolve()
    # Require the resolved path to be within the workspace
    try:
        target.relative_to(workspace)
    except ValueError:
        raise ValueError(
            f"Path '{requested}' escapes workspace '{workspace}'"
        ) from None
    return target


# ── Read-only workspace tools ──────────────────────────────────────────────


async def tool_read_file(path: str, workspace: Path | None = None) -> str:
    """Read a file from the workspace and return its contents.

    Args:
        path: Relative or absolute path within the workspace.
        workspace: The workspace root (defaults to ANTIGONA_WORKSPACE env).

    Returns:
        The file content as a string.

    Raises:
        FileNotFoundError: If the file does not exist.
        ValueError: If the path escapes the workspace.
    """
    ws = workspace or _resolve_workspace()
    target = _safe_workspace_path(path, ws)

    if not target.is_file():
        raise FileNotFoundError(f"File not found: {path} (resolved: {target})")

    content = target.read_text(encoding="utf-8")
    return content


async def tool_check_file(path: str, workspace: Path | None = None) -> str:
    """Check whether a file exists in the workspace.

    Args:
        path: Relative or absolute path within the workspace.
        workspace: The workspace root.

    Returns:
        A JSON-like string describing existence and metadata.
    """
    ws = workspace or _resolve_workspace()
    try:
        target = _safe_workspace_path(path, ws)
    except ValueError:
        return f'{{"exists": false, "path": "{path}", "error": "path escaped workspace"}}'

    if not target.exists():
        return f'{{"exists": false, "path": "{path}"}}'

    stat = target.stat()
    return (
        f'{{"exists": true, "path": "{path}", "is_file": {str(target.is_file()).lower()}, '
        f'"is_dir": {str(target.is_dir()).lower()}, "size": {stat.st_size}}}'
    )


async def tool_list_dir(path: str, workspace: Path | None = None) -> str:
    """List the contents of a directory in the workspace.

    Args:
        path: Relative or absolute directory path within the workspace.
        workspace: The workspace root.

    Returns:
        A newline-separated listing of directory entries.
    """
    ws = workspace or _resolve_workspace()
    target = _safe_workspace_path(path, ws)

    if not target.is_dir():
        raise NotADirectoryError(f"Not a directory: {path} (resolved: {target})")

    entries = sorted(
        str(e.relative_to(ws)) if str(e).startswith(str(ws)) else e.name
        for e in target.iterdir()
    )
    return "\n".join(entries) if entries else "(empty directory)"


# ── Tool definitions (OpenAI-compatible format for the LLM) ─────────────────

READ_ONLY_TOOLS: list[dict[str, Any]] = [
    {
        "name": "tool_read_file",
        "description": (
            "Read the full contents of a text file from the project workspace. "
            "If the path does not exist, the tool will return an error. "
            "Use this to inspect source code, configuration files, or any text file."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file, relative to the workspace root.",
                }
            },
            "required": ["path"],
        },
    },
    {
        "name": "tool_check_file",
        "description": (
            "Check whether a file or directory exists in the project workspace. "
            "Returns metadata such as size and type. Useful to verify a path "
            "before reading it, or to discover the correct path when a file "
            "was moved or renamed."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to check, relative to the workspace root.",
                }
            },
            "required": ["path"],
        },
    },
    {
        "name": "tool_list_dir",
        "description": (
            "List the contents of a directory in the project workspace. "
            "Returns one entry per line. Use this to explore the project "
            "structure, find files, or navigate to the correct location "
            "when a requested path was not found."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Directory path, relative to the workspace root.",
                }
            },
            "required": ["path"],
        },
    },
]

# System prompt injected to guide read-only task behaviour with auto-repair.
# The workspace line is derived from the canonical resolver (DEFAULT_WORKSPACE,
# i.e. the project root) instead of a hardcoded home literal (B22): the model
# must be told the *real* workspace root, which is not a fixed path.
READ_ONLY_SYSTEM_PROMPT = f"""You are an autonomous read-only file-system agent.

You have access to three tools:
- tool_read_file(path) — read a file's contents
- tool_check_file(path) — check if a file/dir exists (with metadata)
- tool_list_dir(path) — list directory contents

Rules:
1. You may ONLY use these three tools — do not attempt write or shell operations.
2. If a tool returns an error (e.g. file not found), use tool_check_file or
   tool_list_dir to discover the correct path, then retry. Do NOT report errors
   to the user until you have exhausted all reasonable alternatives.
3. Your job is to silently resolve read requests. The user should only see the
   final result, never internal error messages.
4. Workspace is {DEFAULT_WORKSPACE}. All paths are relative to this root.
5. When you have the information the user requested, provide a clear summary.
"""

# ── Read-only task identification ──────────────────────────────────────────

READ_ONLY_TOOL_NAMES = frozenset({"tool_read_file", "tool_check_file", "tool_list_dir"})
READ_ONLY_TASK_NAMES = frozenset({"file_read", "file_check", "file_list", "read_file", "check_file", "list_dir"})

# ── Tool adapter: create a tool_map from a workspace reference ──────────────


def build_readonly_tool_map(workspace: Path | None = None) -> dict[str, Any]:
    """Build the tool_map for read-only workspace operations.

    Returns a dict mapping tool name → async callable with the workspace
    path pre-bound.
    """
    ws = workspace or _resolve_workspace()

    async def _read_file(path: str) -> str:
        return await tool_read_file(path, workspace=ws)

    async def _check_file(path: str) -> str:
        return await tool_check_file(path, workspace=ws)

    async def _list_dir(path: str) -> str:
        return await tool_list_dir(path, workspace=ws)

    return {
        "tool_read_file": _read_file,
        "tool_check_file": _check_file,
        "tool_list_dir": _list_dir,
    }


def is_readonly_task(task_name: str) -> bool:
    """Return True if the given task/tool name is a read-only operation."""
    return task_name in READ_ONLY_TOOL_NAMES or task_name in READ_ONLY_TASK_NAMES


def is_write_task(task_name: str) -> bool:
    """Return True if the given task name is a write/shell operation."""
    return task_name in {
        "sandbox.shell",
        "workspace.write_text",
        "write_file",
        "shell",
        "tool_write_file",
    }


# ── TurnWorker ─────────────────────────────────────────────────────────────


class TurnWorker:
    """Executes read-only TaskFlow goals through the TurnEngine.

    The TurnWorker wraps a :class:`TurnEngine` with workspace-scoped
    read-only tools (read_file, check_file, list_dir) and a system prompt
    that guides the model to auto-recover from errors (e.g. wrong paths).

    Args:
        base_url: OpenAI-compatible API base URL.
        api_key: API key.
        model: Model identifier.
        workspace_path: Absolute path to the workspace root.
            Falls back to ``ANTIGONA_WORKSPACE`` env, then the canonical
            project root (``antigona.core.paths.project_root()``).
        timeout_seconds: Provider request timeout.
        max_retries: Transient error retries for the provider.
        extra_system_prompt: Optional extra system prompt text appended
            to the default read-only prompt.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        *,
        workspace_path: str | None = None,
        timeout_seconds: int = 120,
        max_retries: int = 2,
        extra_system_prompt: str | None = None,
        provider: ProviderAdapter | None = None,
        turn_engine: TurnEngine | None = None,
    ) -> None:
        self._workspace = _resolve_workspace(workspace_path)

        if turn_engine is not None:
            self._engine = turn_engine
        elif provider is not None:
            system_prompt = READ_ONLY_SYSTEM_PROMPT
            if extra_system_prompt:
                system_prompt = f"{system_prompt}\n\n{extra_system_prompt}"
            self._engine = TurnEngine(
                provider=provider,
                extra_system_prompt=system_prompt,
            )
        else:
            actual_provider = ProviderAdapter(
                config=ProviderAdapterConfig(
                    base_url=base_url,
                    api_key=api_key,
                    model=model,
                    timeout_seconds=timeout_seconds,
                    max_retries=max_retries,
                )
            )
            system_prompt = READ_ONLY_SYSTEM_PROMPT
            if extra_system_prompt:
                system_prompt = f"{system_prompt}\n\n{extra_system_prompt}"
            self._engine = TurnEngine(
                provider=actual_provider,
                extra_system_prompt=system_prompt,
            )
        self._tool_map = build_readonly_tool_map(self._workspace)

    @property
    def engine(self) -> TurnEngine:
        """The underlying TurnEngine instance."""
        return self._engine

    @property
    def workspace(self) -> Path:
        """The resolved workspace root path."""
        return self._workspace

    async def execute_task(
        self,
        flow_id: str,
        goal: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        budget: TurnBudget | None = None,
    ) -> TurnResult:
        """Execute a read-only task through the TurnEngine.

        Args:
            flow_id: Unique identifier for the flow (used for logging).
            goal: The high-level goal for this turn.
            messages: Initial message history (from the Gateway or caller).
            tools: Tool descriptions the model may call.
                Defaults to :data:`READ_ONLY_TOOLS` if omitted.
            budget: Budget constraints (defaults to
                ``TurnBudget(max_turns=15, max_tool_calls=30, max_duration_seconds=120)``).

        Returns:
            A :class:`TurnResult` describing the outcome.
        """
        effective_budget = budget or TurnBudget(
            max_turns=15,
            max_tool_calls=30,
            max_duration_seconds=120,
        )
        effective_tools = tools if tools is not None else READ_ONLY_TOOLS

        LOGGER.info(
            "TurnWorker executing flow=%s goal=%s budget=%s",
            flow_id,
            goal[:80],
            effective_budget,
        )

        result = await self._engine.run_turn(
            goal=goal,
            messages=messages,
            available_tools=effective_tools,
            budget=effective_budget,
            tool_map=self._tool_map,
        )

        if result.success:
            LOGGER.info(
                "TurnWorker flow=%s completed: turns=%d tool_calls=%d",
                flow_id,
                result.turns_used,
                result.tool_calls_made,
            )
        else:
            LOGGER.warning(
                "TurnWorker flow=%s failed: turns=%d tool_calls=%d error=%s",
                flow_id,
                result.turns_used,
                result.tool_calls_made,
                result.error,
            )

        return result

    async def close(self) -> None:
        """Close the underlying provider's HTTP client."""
        await self._engine.provider.close()


__all__ = [
    "READ_ONLY_SYSTEM_PROMPT",
    "READ_ONLY_TOOLS",
    "READ_ONLY_TOOL_NAMES",
    "READ_ONLY_TASK_NAMES",
    "TurnWorker",
    "build_readonly_tool_map",
    "is_readonly_task",
    "is_write_task",
    "tool_check_file",
    "tool_list_dir",
    "tool_read_file",
]
