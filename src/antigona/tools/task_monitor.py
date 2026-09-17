"""Task monitor — live progress visibility for Antigona's executions.

Tracks active tasks/steps so the user can see what Antigona is doing in
real time. Provides a snapshot for the /monitor command and a helper to
render a progress bar for Telegram messages.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

# Global registry of active executions (chat_id -> list of ActiveTask)
_active_tasks: dict[int, list[ActiveTask]] = {}


@dataclass
class ActiveTask:
    """A single tracked execution (one LLM action or one PLAN step)."""

    chat_id: int
    label: str
    started_at: float = field(default_factory=time.time)
    status: str = "running"  # running | done | failed
    detail: str = ""

    def elapsed(self) -> float:
        return time.time() - self.started_at


def start_task(chat_id: int, label: str) -> ActiveTask:
    """Register a new active task and return its handle."""
    task = ActiveTask(chat_id=chat_id, label=label)
    _active_tasks.setdefault(chat_id, []).append(task)
    # Keep only last 10 per chat
    if len(_active_tasks[chat_id]) > 10:
        _active_tasks[chat_id] = _active_tasks[chat_id][-10:]
    return task


def finish_task(task: ActiveTask, ok: bool, detail: str = "") -> None:
    """Mark a task finished."""
    task.status = "done" if ok else "failed"
    task.detail = detail


def get_active(chat_id: int) -> list[ActiveTask]:
    """Return active (non-finished) tasks for a chat."""
    return [t for t in _active_tasks.get(chat_id, []) if t.status == "running"]


def render_progress(chat_id: int) -> str:
    """Render a compact progress block for /monitor."""
    tasks = _active_tasks.get(chat_id, [])
    if not tasks:
        return "📭 Нет активных задач."
    lines = ["📊 <b>Мониторинг задач</b>"]
    for _i, t in enumerate(tasks[-8:], 1):
        icon = (
            "⏳" if t.status == "running"
            else "✅" if t.status == "done"
            else "❌"
        )
        secs = int(t.elapsed())
        lines.append(f"{icon} {t.label} ({secs}s)")
        if t.detail:
            lines.append(f"    └ {t.detail}")
    return "\n".join(lines)


def render_bar(fraction: float, width: int = 10) -> str:
    """Render a text progress bar like ▓▓▓▓▓░░░░░."""
    filled = int(width * max(0.0, min(1.0, fraction)))
    return "▓" * filled + "░" * (width - filled)
