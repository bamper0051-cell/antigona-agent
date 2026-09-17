"""TaskRuntime — multi-step sequential task execution with plan persistence.

The runtime manages a sequence of actions (steps) derived from an LLM-generated
plan. It supports:
  - Creating a Task from an LLM PLAN/STEP response
  - Executing steps one at a time (auto-continue)
  - Status tracking and cancellation
  - JSON-based persistence across restarts

Plan format (LLM output):
    PLAN|Название задачи
    STEP|WRITE_FILE|/path/to/file|content
    STEP|SEND_FILE|/path/to/file
    STEP|RUN_SHELL|command

Russian colon format (recommended for Russian persona):
    ПЛАН: Название задачи
    ШАГ 1: WRITE_FILE|/path|content
    ШАГ 2: SEND_FILE|/path
"""

from __future__ import annotations

import json
import re
import sys
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from antigona.core import paths
from antigona.ownership.epoch import FenceDeniedError, OwnershipContext
from antigona.ownership.wiring import enforce_write_fence
from antigona.path_boundary import has_unsafe_relative_path_syntax
from antigona.worker.tools.common import WorkspaceGuard

# ─── Exceptions ────────────────────────────────────────────────────────────────


class PathBoundaryViolation(ValueError):
    """Raised when an action or persistence path violates workspace/boundary fence."""


class InvalidTaskIdError(PathBoundaryViolation):
    """Raised when a task ID fails strict allowlist or boundary containment."""


# ─── Constants ─────────────────────────────────────────────────────────────────

TASK_PERSIST_DIR: str = str(paths.tasks_dir())
_TASK_ID_RE: re.Pattern[str] = re.compile(r"^[A-Za-z0-9_-]+$")

# ─── Enums ─────────────────────────────────────────────────────────────────────


class TaskStatus(StrEnum):
    """Task lifecycle status."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


class ActionType(StrEnum):
    """Action types understood by the step executor."""

    WRITE_FILE = "WRITE_FILE"
    SEND_FILE = "SEND_FILE"
    RUN_SHELL = "RUN_SHELL"
    CONFIGURE_KEY = "CONFIGURE_KEY"
    WEB_SEARCH = "WEB_SEARCH"
    READ_FILE = "READ_FILE"
    SEARCH_FILES = "SEARCH_FILES"
    RUN_CODE = "RUN_CODE"


# ─── Dataclasses ───────────────────────────────────────────────────────────────


@dataclass
class TaskStep:
    """A single step within a multi-step task.

    Attributes:
        id: Unique step ID.
        action_type: The type of action (WRITE_FILE, SEND_FILE, RUN_SHELL).
        action_data: Parsed action data dict (path, content, command).
        status: Step execution status.
        result: Optional result message after execution.
        error: Optional error message.
    """

    id: str
    action_type: str
    action_data: dict[str, Any]
    status: str = "pending"
    result: str = ""
    error: str = ""


@dataclass
class Task:
    """A multi-step task with a goal and sequence of steps.

    Attributes:
        id: Unique task ID.
        goal: The user's original goal description.
        steps: Ordered list of TaskStep.
        status: Overall task status.
        created_at: Unix timestamp of creation.
        completed_at: Unix timestamp of completion (or None).
        current_step_index: Index of the currently executing step.
    """

    id: str
    goal: str
    steps: list[dict[str, Any]]  # serializable step dicts
    status: str = "pending"
    created_at: float = 0.0
    completed_at: float | None = None
    current_step_index: int = 0


# ─── Helpers ──────────────────────────────────────────────────────────────────


def step_action_type(step: dict[str, Any]) -> str:
    """Get the action type from a serialised step dict."""
    val = step.get("action_type", "")
    return str(val) if val is not None else ""


# ─── Plan parser ───────────────────────────────────────────────────────────────


class PlanParser:
    """Parses PLAN/STEP commands from LLM output.

    Supports both English and Russian formats.

    Pipe format (backward compat)::

        PLAN|Название задачи
        STEP|WRITE_FILE|/path|content
        STEP|SEND_FILE|/path

    Colon format (recommended for Russian persona)::

        ПЛАН: Название задачи
        ШАГ 1: WRITE_FILE|/path|content
        ШАГ 2: SEND_FILE|/path
    """

    # PLAN|Title or ПЛАН|Название  (pipe)
    _PLAN_PIPE_RE = re.compile(
        r"(?:PLAN|ПЛАН)\|(.+)", re.IGNORECASE | re.MULTILINE
    )
    # ПЛАН: Title  (colon)
    _PLAN_COLON_RE = re.compile(
        r"(?:ПЛАН)\s*:\s*(.+)", re.IGNORECASE | re.MULTILINE
    )

    # STEP|ACTION|args  (pipe)
    _STEP_PIPE_RE = re.compile(
        r"(?:STEP|ШАГ)\|(\w+)\|(.+)", re.IGNORECASE | re.MULTILINE
    )
    # ШАГ N: ACTION|args  (colon)
    _STEP_COLON_RE = re.compile(
        r"(?:ШАГ)\s*\d*\s*:\s*(\w+)\|(.+)", re.IGNORECASE | re.MULTILINE
    )

    # Alias for backward-compat has_plan checks
    _PLAN_RE = _PLAN_PIPE_RE

    @classmethod
    def _find_plan_line(cls, text: str) -> str | None:
        """Find a PLAN/ПЛАН line in either pipe or colon format."""
        # Try pipe format first: PLAN|... or ПЛАН|...
        m = cls._PLAN_PIPE_RE.search(text)
        if m:
            return m.group(1).strip()
        # Try colon format: ПЛАН: ...
        m = cls._PLAN_COLON_RE.search(text)
        if m:
            return m.group(1).strip()
        return None

    @classmethod
    def parse(cls, text: str) -> tuple[str, list[dict[str, Any]]]:
        """Parse PLAN/STEP commands from LLM output.

        Args:
            text: LLM response text.

        Returns:
            Tuple of (goal, list of step dicts).
            Each step dict: {"action_type": str, ...}
            Returns ("", []) if no PLAN found.
        """
        goal = cls._find_plan_line(text) or ""
        steps: list[dict[str, Any]] = []

        # Parse pipe-format steps: STEP|ACTION|... or ШАГ|ACTION|...
        for m in cls._STEP_PIPE_RE.finditer(text):
            action_type = m.group(1).upper()
            rest = m.group(2).strip()
            step = cls._parse_step_data(action_type, rest)
            if step:
                steps.append(step)

        # Parse colon-format steps: ШАГ N: ACTION|...
        for m in cls._STEP_COLON_RE.finditer(text):
            action_type = m.group(1).upper()
            rest = m.group(2).strip()
            step = cls._parse_step_data(action_type, rest)
            if step:
                steps.append(step)

        return goal, steps

    @classmethod
    def _parse_step_data(
        cls, action_type: str, rest: str
    ) -> dict[str, Any] | None:
        """Parse a single STEP line into a step dict."""
        if action_type == "WRITE_FILE":
            # WRITE_FILE|/path|content
            pipe_idx = rest.find("|")
            if pipe_idx >= 0:
                path = rest[:pipe_idx].strip()
                content = rest[pipe_idx + 1 :]
                return {
                    "action_type": "WRITE_FILE",
                    "path": path,
                    "content": content,
                }
            else:
                return {
                    "action_type": "WRITE_FILE",
                    "path": rest,
                    "content": "",
                }
        elif action_type == "SEND_FILE":
            return {
                "action_type": "SEND_FILE",
                "path": rest,
            }
        elif action_type == "RUN_SHELL":
            return {
                "action_type": "RUN_SHELL",
                "command": rest,
            }
        elif action_type == "READ_FILE":
            return {
                "action_type": "READ_FILE",
                "path": rest,
            }
        elif action_type == "SEARCH_FILES":
            pipe_idx = rest.find("|")
            if pipe_idx >= 0:
                pattern = rest[:pipe_idx].strip()
                search_path = rest[pipe_idx + 1 :].strip()
                return {
                    "action_type": "SEARCH_FILES",
                    "content": pattern,
                    "path": search_path,
                }
            return {
                "action_type": "SEARCH_FILES",
                "content": rest,
                "path": "",
            }
        elif action_type == "RUN_CODE":
            pipe_idx = rest.find("|")
            if pipe_idx >= 0:
                language = rest[:pipe_idx].strip()
                code = rest[pipe_idx + 1 :]
                return {
                    "action_type": "RUN_CODE",
                    "path": language,
                    "content": code,
                }
            return {
                "action_type": "RUN_CODE",
                "path": "python",
                "content": rest,
            }
        elif action_type == "CONFIGURE_KEY":
            pipe_idx = rest.find("|")
            if pipe_idx >= 0:
                provider = rest[:pipe_idx].strip()
                key = rest[pipe_idx + 1 :].strip()
                return {
                    "action_type": "CONFIGURE_KEY",
                    "path": provider,
                    "content": key,
                }
            return {
                "action_type": "CONFIGURE_KEY",
                "path": rest,
                "content": "",
            }
        return None

    @classmethod
    def has_plan(cls, text: str) -> bool:
        """Check if text contains a PLAN/ПЛАН command in any format.

        Detects:
            PLAN|... or ПЛАН|... (pipe)
            ПЛАН: ...  (colon)
        """
        if cls._PLAN_PIPE_RE.search(text):
            return True
        if cls._PLAN_COLON_RE.search(text):
            return True
        return False


# ─── Persistence ───────────────────────────────────────────────────────────────


def _tasks_dir() -> Path:
    """Get or create the tasks persistence directory."""
    d = Path(TASK_PERSIST_DIR).resolve()
    d.mkdir(parents=True, exist_ok=True)
    return d


def _validate_task_id(task_id: str) -> bool:
    """Strict allowlist validation for task IDs."""
    if not isinstance(task_id, str) or not task_id:
        return False
    if not _TASK_ID_RE.fullmatch(task_id):
        return False
    if has_unsafe_relative_path_syntax(task_id):
        return False
    return True


def _resolve_task_path(task_id: str) -> Path:
    """Resolve and validate a task JSON path strictly inside TASK_PERSIST_DIR."""
    if not _validate_task_id(task_id):
        raise InvalidTaskIdError(f"Invalid task ID format: {task_id!r}")
    tasks_dir = _tasks_dir()
    target = (tasks_dir / f"{task_id}.json").resolve(strict=False)
    try:
        target.relative_to(tasks_dir)
    except ValueError as exc:
        raise PathBoundaryViolation(f"Task ID escapes tasks dir: {task_id!r}") from exc
    # Symlink component containment
    candidate = tasks_dir / f"{task_id}.json"
    if candidate.is_symlink():
        raise PathBoundaryViolation(f"Symlink task file forbidden: {task_id!r}")
    return candidate


def _save_task(task: Task) -> None:
    """Persist a Task to JSON."""
    path = _resolve_task_path(task.id)
    data = asdict(task)
    data["steps"] = task.steps  # already dicts
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _load_task(task_id: str) -> Task | None:
    """Load a Task from JSON."""
    try:
        path = _resolve_task_path(task_id)
    except (PathBoundaryViolation, ValueError):
        return None
    if not path.exists() or not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return Task(**data)
    except (json.JSONDecodeError, KeyError, TypeError, OSError):
        return None


def _list_tasks() -> list[Task]:
    """List all persisted tasks sorted by creation time (newest first)."""
    tasks: list[Task] = []
    tasks_dir = _tasks_dir()
    for f in sorted(tasks_dir.glob("*.json"), reverse=True):
        if f.is_symlink():
            continue
        task_id = f.stem
        if not _validate_task_id(task_id):
            continue
        task = _load_task(task_id)
        if task is not None:
            tasks.append(task)
    return tasks


def _delete_task(task_id: str) -> None:
    """Delete a persisted task."""
    try:
        path = _resolve_task_path(task_id)
    except (PathBoundaryViolation, ValueError):
        return
    if path.exists() and path.is_file():
        path.unlink()


# ─── Action Workspace Resolver & Validator ───────────────────────────────────


def _default_action_workspace() -> Path:
    """Resolve the authoritative durable external action workspace for TaskRuntime.

    Resolution order:
      1. ANTIGONA_TASK_WORKSPACE / ANTIGONA_TASK_ACTION_WORKSPACE (dedicated env override)
      2. <ANTIGONA_STATE_ROOT>/workspace/tasks if ANTIGONA_STATE_ROOT is configured
      3. /var/lib/antigona/workspace/tasks (canonical durable external root)
    """
    return paths.runtime_path(
        ("ANTIGONA_TASK_WORKSPACE", "ANTIGONA_TASK_ACTION_WORKSPACE"),
        "workspace/tasks",
        lambda: Path("/var/lib/antigona/workspace/tasks"),
        what="task action workspace directory",
    )


def _validate_action_workspace(ws: Path | str) -> Path:
    """Validate action workspace boundary before mkdir or operations.

    Enforces:
      - Absolute path.
      - Never the filesystem root or the home directory (``paths.home_dir()``).
      - Never inside project repository root (no source tree mutation).
      - Never contains .git or .venv directory.
    """
    resolved = Path(ws).resolve()
    if not resolved.is_absolute():
        raise PathBoundaryViolation(f"Workspace root must be absolute: {resolved}")
    # Deny the filesystem root and the *real* home directory. The home entry
    # used to be the literal "/root": on a host where HOME/ANTIGONA_HOME_DIR
    # != /root the actual home was NOT denied, so a task workspace could be
    # rooted at the user's home. Derive it from the single home resolver
    # (ADR-007). On this host home_dir() == "/root", so behaviour is unchanged.
    forbidden_home = paths.home_dir().resolve()
    if resolved == Path("/") or resolved == forbidden_home:
        raise PathBoundaryViolation(
            f"Workspace root cannot be the filesystem root or the home directory "
            f"({forbidden_home}): {resolved}"
        )
    try:
        proj_root = paths.project_root().resolve()
        if resolved == proj_root or proj_root in resolved.parents:
            raise PathBoundaryViolation(
                f"Workspace root cannot be inside project root ({proj_root}): {resolved}"
            )
    except PathBoundaryViolation:
        raise
    except Exception:
        pass

    try:
        if resolved.exists():
            if (resolved / ".git").exists():
                raise PathBoundaryViolation(f"Workspace root cannot contain .git: {resolved}")
            if (resolved / ".venv").exists():
                raise PathBoundaryViolation(f"Workspace root cannot contain .venv: {resolved}")
    except PathBoundaryViolation:
        raise
    except OSError:
        pass
    return resolved


# ─── TaskRuntime ───────────────────────────────────────────────────────────────


class TaskRuntime:
    """Manages multi-step task lifecycle: create, execute, track, cancel.

    Thread-safe. Persists tasks to JSON in ``TASK_PERSIST_DIR``.
    """

    def __init__(
        self,
        *,
        workspace: Path | str | None = None,
        sandbox_runtime: str = "",
        shell_timeout: float = 30.0,
        ownership: OwnershipContext | None = None,
    ) -> None:
        self._lock = threading.Lock()
        # In-memory cache keyed by task_id
        self._tasks: dict[str, Task] = {}
        self._ownership: OwnershipContext | None = ownership
        # Authoritative action workspace (dedicated durable external root)
        if workspace is not None:
            target_ws = Path(workspace)
        else:
            target_ws = _default_action_workspace()
        self._workspace: Path = _validate_action_workspace(target_ws)
        try:
            self._workspace.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        try:
            self._guard: WorkspaceGuard | None = WorkspaceGuard(self._workspace)
        except OSError:
            self._guard = None
        # Default sandbox runtime / shell timeout for RUN_SHELL/RUN_CODE.
        # Overridable by callers that wire a Docker sandbox.
        self._sandbox_runtime: str = sandbox_runtime
        self._shell_timeout: float = shell_timeout

    def bind_ownership(self, ownership: OwnershipContext | None) -> None:
        """Bind a live fencing token to this runtime (DF-WO2-003-full)."""
        self._ownership = ownership

    @property
    def workspace(self) -> Path:
        """The authoritative action workspace directory."""
        return self._workspace

    def _resolve_action_path(self, path: str) -> Path:
        """Resolve and validate an action path strictly within the authoritative workspace.

        Enforces:
          - Only relative safe paths (no absolute, dot/dotdot, backslash/Windows, controls, percent).
          - No symlink escape or lexical sibling.
          - Fail-closed before any filesystem mutation or read.
        """
        if not isinstance(path, str) or not path.strip():
            raise PathBoundaryViolation("Path cannot be empty")
        if has_unsafe_relative_path_syntax(path):
            raise PathBoundaryViolation(f"Unsafe relative path syntax: {path}")
        raw = Path(path)
        if raw.is_absolute() or not raw.parts:
            raise PathBoundaryViolation(f"Absolute path forbidden: {path}")
        if any(part in {".", "..", ""} for part in raw.parts):
            raise PathBoundaryViolation(f"Path traversal forbidden: {path}")

        candidate = (self._workspace / raw).resolve(strict=False)
        try:
            candidate.relative_to(self._workspace)
        except ValueError as exc:
            raise PathBoundaryViolation(f"Path escapes workspace: {path}") from exc

        current = self._workspace
        for part in raw.parts:
            current = current / part
            try:
                if current.is_symlink():
                    raise PathBoundaryViolation(f"Symlink path component is forbidden: {path}")
            except OSError:
                pass

        return candidate

    # ── Create ──────────────────────────────────────────────────────────────

    def create_task_from_llm(self, llm_text: str) -> Task | None:
        """Parse LLM PLAN/STEP output and create a Task.

        Args:
            llm_text: LLM response text containing PLAN and STEP commands.

        Returns:
            The created Task, or None if no PLAN was found.
        """
        goal, steps = PlanParser.parse(llm_text)
        if not goal or not steps:
            return None

        return self._create_task(goal, steps)

    def create_task(self, goal: str, steps: list[dict[str, Any]]) -> Task:
        """Create a Task directly with given goal and steps.

        Args:
            goal: The task goal.
            steps: List of step dicts.

        Returns:
            The created Task.
        """
        return self._create_task(goal, steps)

    def _create_task(self, goal: str, steps: list[dict[str, Any]]) -> Task:
        """Internal: create, cache, and persist a Task."""
        task = Task(
            id=self._generate_id(),
            goal=goal,
            steps=steps,
            status=TaskStatus.PENDING,
            created_at=time.time(),
            current_step_index=0,
        )
        # Assign step IDs
        for i, step in enumerate(task.steps):
            step["id"] = f"step-{i + 1}"
            step["status"] = "pending"
            step["result"] = ""
            step["error"] = ""

        with self._lock:
            self._tasks[task.id] = task
            _save_task(task)

        return task

    # ── Execute ─────────────────────────────────────────────────────────────

    def execute_next_step(
        self, task_id: str
    ) -> dict[str, Any] | None:
        """Execute the next pending step of a task.

        Args:
            task_id: The task ID.

        Returns:
            Dict with step execution result, or None if task/step not found.
            Result keys: step_index, total_steps, action_type, success,
                         message, error, task_status
        """
        with self._lock:
            task = self._get_task(task_id)
            if task is None:
                return None

            steps = task.steps

            # Find the first pending step
            step_index = -1
            for i, s in enumerate(steps):
                if s.get("status") == "pending":
                    step_index = i
                    break

            if step_index < 0:
                # All steps done (or none pending)
                if all(s.get("status") == "completed" for s in steps):
                    task.status = TaskStatus.COMPLETED
                    task.completed_at = time.time()
                    _save_task(task)
                return {
                    "step_index": -1,
                    "total_steps": len(steps),
                    "action_type": "",
                    "success": False,
                    "message": "Нет ожидающих шагов.",
                    "error": "No pending steps",
                    "task_status": task.status,
                    "completed": task.status == "completed",
                }

            step = steps[step_index]
            step["status"] = "running"
            task.status = TaskStatus.RUNNING
            task.current_step_index = step_index
            _save_task(task)

        # Execute the step (outside lock to allow concurrent access)
        result = self._do_execute(step)

        with self._lock:
            task = self._get_task(task_id)
            if task is None:
                return None
            steps = task.steps

            # Re-fetch the step
            if step_index < len(steps):
                current_step = steps[step_index]
                if result["success"]:
                    current_step["status"] = "completed"
                    current_step["result"] = result["message"]
                else:
                    current_step["status"] = "failed"
                    current_step["error"] = result.get("error", "")

                # Check if all steps done
                all_done = all(
                    s.get("status") in ("completed", "failed")
                    for s in steps
                )
                if all_done:
                    task.status = TaskStatus.COMPLETED
                    task.completed_at = time.time()
                _save_task(task)

            return {
                "step_index": step_index,
                "total_steps": len(steps),
                "action_type": step.get("action_type", ""),
                "success": result["success"],
                "message": result["message"],
                "error": result.get("error", ""),
                "task_status": task.status,
                "completed": task.status == "completed",
            }

    def _do_execute(
        self, step: dict[str, Any]
    ) -> dict[str, Any]:
        """Execute a single step synchronously.

        Args:
            step: Step dict with action_type and action_data.

        Returns:
            Result dict with "success" and "message" keys.
        """
        action_type = step.get("action_type", "").upper()

        try:
            if action_type == "WRITE_FILE":
                return self._exec_write_file(step)
            elif action_type == "SEND_FILE":
                return self._exec_send_file(step)
            elif action_type == "RUN_SHELL":
                return self._exec_run_shell(step)
            elif action_type == "READ_FILE":
                return self._exec_read_file(step)
            elif action_type == "SEARCH_FILES":
                return self._exec_search_files(step)
            elif action_type == "RUN_CODE":
                return self._exec_run_code(step)
            elif action_type == "CONFIGURE_KEY":
                return self._exec_configure_key(step)
            else:
                return {
                    "success": False,
                    "message": f"Неизвестный тип действия: {action_type}",
                    "error": f"Unknown action type: {action_type}",
                }
        except PathBoundaryViolation as exc:
            return {
                "success": False,
                "message": f"Ошибка безопасности пути: {exc}",
                "error": "PathBoundaryViolation",
            }
        except Exception as e:
            return {
                "success": False,
                "message": f"Ошибка: {e}",
                "error": str(e),
            }

    def _exec_write_file(
        self, step: dict[str, Any]
    ) -> dict[str, Any]:
        """Execute a WRITE_FILE step."""
        path = step.get("path", "")
        content = step.get("content", "")
        try:
            enforce_write_fence(getattr(self, "_ownership", None), "task_runtime.write_file")
        except FenceDeniedError as exc:
            return {
                "success": False,
                "message": f"Ошибка владения рабочей областью: {exc.check.reason}",
                "error": f"protected write denied: {exc.check.reason}",
            }
        try:
            filepath = self._resolve_action_path(path)
            filepath.parent.mkdir(parents=True, exist_ok=True)
            filepath.write_text(content, encoding="utf-8")
            return {
                "success": True,
                "message": f"Файл {path} создан ({filepath.stat().st_size} байт).",
            }
        except PathBoundaryViolation as exc:
            return {
                "success": False,
                "message": f"Ошибка безопасности пути: {exc}",
                "error": "PathBoundaryViolation",
            }
        except OSError as e:
            return {
                "success": False,
                "message": f"Ошибка записи файла: {e}",
                "error": str(e),
            }

    def _exec_send_file(
        self, step: dict[str, Any]
    ) -> dict[str, Any]:
        """Execute a SEND_FILE step (mark as ready for sending)."""
        path = step.get("path", "")
        try:
            filepath = self._resolve_action_path(path)
        except PathBoundaryViolation as exc:
            return {
                "success": False,
                "message": f"Ошибка безопасности пути: {exc}",
                "error": "PathBoundaryViolation",
            }
        try:
            exists = filepath.exists()
            is_file = filepath.is_file() if exists else False
        except OSError:
            exists = False
            is_file = False
        if not exists or not is_file:
            return {
                "success": False,
                "message": f"Файл {path} не найден.",
                "error": "File not found",
            }
        return {
            "success": True,
            "message": f"Файл {path} готов к отправке.",
        }

    def _exec_run_shell(
        self, step: dict[str, Any]
    ) -> dict[str, Any]:
        """Execute a RUN_SHELL step.

        Host shell execution is completely forbidden for TaskRuntime (fail-closed).
        Execution is ONLY permitted inside an isolated Docker sandbox (sandbox_runtime="docker").
        If sandbox_runtime is missing, empty, unknown, malformed, or explicitly "host",
        the step is denied immediately BEFORE any shlex/subprocess execution.
        If Docker sandbox is unavailable or fails, execution fails closed with zero host fallback.
        """
        import shlex

        # 1. Strict sandbox runtime validation — fail closed BEFORE shlex / subprocess
        raw_sandbox = step.get("sandbox_runtime")
        if raw_sandbox is None:
            raw_sandbox = step.get("sandbox")
        if raw_sandbox is None:
            raw_sandbox = self._sandbox_runtime

        if not isinstance(raw_sandbox, str) or raw_sandbox.strip().lower() != "docker":
            return {
                "success": False,
                "message": "Выполнение RUN_SHELL на хосте запрещено. Требуется sandbox_runtime='docker'.",
                "error": "HostExecutionForbidden",
            }

        # 2. Command validation
        command = step.get("command", "")
        if not isinstance(command, str) or not command.strip():
            return {"success": False, "message": "Пустая команда.", "error": "Empty"}
        command = command.strip()

        try:
            parts = shlex.split(command)
        except ValueError as exc:
            return {
                "success": False,
                "message": f"Некорректная команда: {exc}",
                "error": "ParseError",
            }
        if not parts:
            return {"success": False, "message": "Пустая команда.", "error": "Empty"}

        # 3. Docker sandbox execution — fail closed, zero host fallback
        try:
            from antigona.sandbox.docker_sandbox import DockerSandboxBackend

            backend = DockerSandboxBackend(timeout_seconds=int(self._shell_timeout or 30))
            result = backend.run(parts)
            if result.exit_code != 0:
                return {
                    "success": False,
                    "message": result.stderr or f"Exit code {result.exit_code}",
                    "error": result.stderr or f"Exit code {result.exit_code}",
                }
            return {
                "success": True,
                "message": (result.stdout or "").strip()[:500] or "OK",
            }
        except Exception as exc:  # noqa: BLE001 - sandbox failure fails closed, zero host fallback
            return {
                "success": False,
                "message": f"Ошибка sandbox: {exc}",
                "error": "SandboxError",
            }

    def _exec_configure_key(
        self, step: dict[str, Any]
    ) -> dict[str, Any]:
        """Execute a CONFIGURE_KEY step."""
        provider = step.get("path", "")
        api_key = step.get("content", "")
        try:
            from antigona.tools.key_manager import configure_full_keyflow

            result = configure_full_keyflow(provider, api_key)
            if result["success"]:
                return {
                    "success": True,
                    "message": f"✅ Ключ {provider} записан и проверен.",
                }
            return {
                "success": False,
                "message": result.get("error", "Ошибка настройки ключа"),
                "error": result.get("error", ""),
            }
        except ImportError:
            return {
                "success": False,
                "message": "Key manager недоступен.",
                "error": "ImportError",
            }

    def _exec_read_file(
        self, step: dict[str, Any]
    ) -> dict[str, Any]:
        """Execute a READ_FILE step."""
        path = step.get("path", "")
        try:
            filepath = self._resolve_action_path(path)
        except PathBoundaryViolation as exc:
            return {
                "success": False,
                "message": f"Ошибка безопасности пути: {exc}",
                "error": "PathBoundaryViolation",
            }
        try:
            exists = filepath.exists()
            is_file = filepath.is_file() if exists else False
        except OSError:
            exists = False
            is_file = False
        if not exists:
            return {
                "success": False,
                "message": f"Файл {path} не найден.",
                "error": "File not found",
            }
        if not is_file:
            return {
                "success": False,
                "message": f"{path} не является файлом.",
                "error": "Not a file",
            }
        try:
            content = filepath.read_text(encoding="utf-8", errors="replace")
            size = filepath.stat().st_size
            preview = content[:3000]
            if len(content) > 3000:
                preview += f"\n\n... [файл обрезан, полный размер {size} байт]"
            return {
                "success": True,
                "message": f"📄 {path} ({size} байт)\n{preview[:500]}",
            }
        except Exception as e:
            return {
                "success": False,
                "message": f"Ошибка чтения файла: {e}",
                "error": str(e),
            }

    def _exec_search_files(
        self, step: dict[str, Any]
    ) -> dict[str, Any]:
        """Execute a SEARCH_FILES step."""
        import re as _re
        import subprocess

        pattern = step.get("content", "")
        raw_search_path = step.get("path", "")
        if not raw_search_path or raw_search_path in (str(self._workspace), "."):
            search_target = self._workspace
        else:
            try:
                search_target = self._resolve_action_path(raw_search_path)
            except PathBoundaryViolation as exc:
                return {
                    "success": False,
                    "message": f"Ошибка безопасности пути: {exc}",
                    "error": "PathBoundaryViolation",
                }
        search_path = str(search_target)
        has_regex = bool(_re.search(r"[\[\]\.\^\\\+\(\)\{\}]", pattern))
        timeout = 15

        try:
            if has_regex or pattern.startswith("."):
                cmd = ["grep", "-r", "--include=*.py", "--include=*.md",
                       "--include=*.txt", "--include=*.json", "--include=*.yaml",
                       "--include=*.yml", "--include=*.toml", "--include=*.cfg",
                       "--include=*.ini", "--include=*.sh",
                       "-l", pattern, search_path]
            else:
                cmd = ["find", search_path, "-maxdepth", "5",
                       "-type", "f", "-name", pattern]
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout,
            )
            output = result.stdout.strip()
            if not output:
                return {
                    "success": True,
                    "message": f"🔍 По паттерну '{pattern}' ничего не найдено в {search_path}.",
                }
            lines = output.split("\n")
            if len(lines) > 30:
                return {
                    "success": True,
                    "message": f"🔍 Найдено {len(lines)} файлов:\n" + "\n".join(lines[:30]) + f"\n... и ещё {len(lines) - 30}",
                }
            return {
                "success": True,
                "message": f"🔍 Найдено {len(lines)} файлов:\n{output}",
            }
        except subprocess.TimeoutExpired:
            return {
                "success": False,
                "message": f"Поиск превысил таймаут ({timeout}с).",
                "error": "Timeout",
            }
        except Exception as e:
            return {
                "success": False,
                "message": f"Ошибка поиска: {e}",
                "error": str(e),
            }

    def _exec_run_code(
        self, step: dict[str, Any]
    ) -> dict[str, Any]:
        """Execute a RUN_CODE step."""
        import subprocess

        language = step.get("path", "python").lower().strip()
        code = step.get("content", "")
        if not code:
            return {
                "success": False,
                "message": "Код пустой.",
                "error": "Empty code",
            }

        blocked = {"shutdown", "reboot", "rm -rf /", "mkfs",
                   "dd if=/dev/zero", "passwd", "iptables -F"}
        code_lower = code.lower()
        for b in blocked:
            if b in code_lower:
                return {
                    "success": False,
                    "message": f"Код содержит заблокированную команду: '{b}'.",
                    "error": "Blocked command",
                }

        try:
            if language in ("python", "py"):
                result = subprocess.run(
                    [sys.executable, "-c", code],
                    capture_output=True, text=True, timeout=30,
                )
            elif language in ("bash", "sh"):
                result = subprocess.run(
                    ["bash", "-c", code], shell=False,
                    capture_output=True, text=True, timeout=30,
                )
            else:
                return {
                    "success": False,
                    "message": f"Неподдерживаемый язык: {language}.",
                    "error": "Unsupported language",
                }

            if result.returncode != 0:
                return {
                    "success": False,
                    "message": result.stderr[:2000] or f"Exit code {result.returncode}",
                    "error": result.stderr,
                }
            output = result.stdout.strip()
            msg = f"```\n{output[:2000]}\n```" if output else "✅ Код выполнен успешно."
            return {
                "success": True,
                "message": msg,
            }
        except subprocess.TimeoutExpired:
            return {
                "success": False,
                "message": "Код превысил таймаут 30с.",
                "error": "Timeout",
            }
        except Exception as e:
            return {
                "success": False,
                "message": str(e),
                "error": str(e),
            }

    # ── Status ──────────────────────────────────────────────────────────────

    def get_status(self, task_id: str) -> str | None:
        """Get a human-readable status string for a task.

        Args:
            task_id: The task ID.

        Returns:
            Status string like "3 из 7 шагов", or None if task not found.
        """
        task = self._get_task(task_id)
        if task is None:
            return None

        steps = task.steps
        total = len(steps)
        completed = sum(
            1 for s in steps if s.get("status") == "completed"
        )
        failed = sum(1 for s in steps if s.get("status") == "failed")

        parts: list[str] = []
        if task.status == TaskStatus.CANCELLED:
            parts.append("🚫 Отменён")
        elif task.status == TaskStatus.COMPLETED:
            parts.append("✅ Завершён")
        elif task.status == TaskStatus.FAILED:
            parts.append("❌ Ошибка")
        else:
            parts.append("▶️ Выполняется")

        parts.append(f"{completed + failed} из {total} шагов")

        if failed > 0:
            parts.append(f"({failed} с ошибками)")

        return f"**{task.goal}**\n{' | '.join(parts)}"

    # ── Cancel ──────────────────────────────────────────────────────────────

    def cancel_task(self, task_id: str) -> bool:
        """Cancel a task by ID.

        Args:
            task_id: The task ID.

        Returns:
            True if cancelled, False if not found.
        """
        with self._lock:
            task = self._get_task(task_id)
            if task is None:
                return False
            if task.status in ("completed", "cancelled"):
                return False
            task.status = TaskStatus.CANCELLED
            _save_task(task)
        return True

    # ── Query ───────────────────────────────────────────────────────────────

    def get_task(self, task_id: str) -> Task | None:
        """Get a task by ID."""
        return self._get_task(task_id)

    def list_active(self) -> list[Task]:
        """List all active (pending or running) tasks."""
        with self._lock:
            all_tasks = _list_tasks()
        return [t for t in all_tasks if t.status in ("pending", "running")]

    def get_task_count(self) -> dict[str, int]:
        """Get task counts by status."""
        all_tasks = _list_tasks()
        counts: dict[str, int] = {}
        for t in all_tasks:
            status = t.status or "unknown"
            counts[status] = counts.get(status, 0) + 1
        return counts

    def get_progress_message(
        self, result: dict[str, Any], task: Task
    ) -> str:
        """Build a progress message for the user.

        Args:
            result: Result from execute_next_step().
            task: The Task object.

        Returns:
            A human-readable progress message.
        """
        step_idx: Any = result.get("step_index", 0)
        total = result.get("total_steps", 0)
        success = result.get("success", False)
        message = str(result.get("message", "") or "")
        completed = result.get("completed", False)
        task_status = str(result.get("task_status", "") or "")

        if step_idx < 0:
            if task_status == "completed":
                # Build final summary
                step_lines: list[str] = []
                for i, s in enumerate(task.steps, 1):
                    st = s.get("status", "")
                    if st == "completed":
                        step_lines.append(
                            f"  ✅ Шаг {i}/{total}: {s.get('action_type', '')} — {s.get('result', '')}"
                        )
                    elif st == "failed":
                        step_lines.append(
                            f"  ❌ Шаг {i}/{total}: {s.get('action_type', '')} — {s.get('error', '')}"
                        )
                successful = sum(
                    1 for s in task.steps if s.get("status") == "completed"
                )
                failed = sum(
                    1 for s in task.steps if s.get("status") == "failed"
                )
                final_line = f"✅ Готово! {successful} шагов выполнено"
                if failed > 0:
                    final_line += f", {failed} с ошибками."
                else:
                    final_line += "."
                return f"{final_line}\n" + "\n".join(step_lines)
            return message

        step_num = step_idx + 1

        if success:
            base = f"✅ Шаг {step_num}/{total}: {message}"
            if completed:
                done_count = sum(
                    1
                    for s in task.steps
                    if s.get("status") == "completed"
                )
                fail_count = sum(
                    1
                    for s in task.steps
                    if s.get("status") == "failed"
                )
                final = f"✅ Готово! {done_count} шагов выполнено"
                if fail_count > 0:
                    final += f", {fail_count} с ошибками."
                else:
                    final += "."
                return f"{base}\n\n{final}"
            else:
                return f"{base}\n⏩ Продолжаю..."
        else:
            return f"❌ Шаг {step_num}/{total}: {message}"

    # ── Internal ────────────────────────────────────────────────────────────

    def _get_task(self, task_id: str) -> Task | None:
        """Get a task from cache or disk."""
        if not _validate_task_id(task_id):
            return None
        if task_id in self._tasks:
            return self._tasks[task_id]
        task = _load_task(task_id)
        if task is not None:
            self._tasks[task_id] = task
        return task

    @staticmethod
    def _generate_id() -> str:
        """Generate a unique task ID."""
        timestamp = int(time.time() * 1000)
        short_uuid = uuid.uuid4().hex[:8]
        return f"task-{timestamp:x}-{short_uuid}"
