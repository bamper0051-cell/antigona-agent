"""Live activity tracking for the CLI presentation layer.

A process-wide tracker of *what the agent is doing right now* (shell commands,
subagent calls, model thinking, flow waits).  The persistent bottom-toolbar
(:mod:`antigona.cli_ui.status_bar`) reads it every refresh cycle to animate a
status line while work is in flight, and collapses back to an idle indicator
when nothing is running.

Clean-room, no Core/Gateway/DB/network dependencies — it is pure in-memory
bookkeeping for the UI.  A separate singleton per process lets both the chat
loop and any delegated sub-task register work without cross-wiring.

Design notes (mirrors the standalone Antigona CLI reference):
  - ``get_tracker()`` returns the process-wide singleton.
  - ``start()/update()/finish()`` mutate the live set.
  - ``list(live_only=True)`` is what the status bar reads; finished activities
    are removed from that view so the bar returns to idle.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


@dataclass
class Activity:
    """A single live activity being rendered in the status bar."""

    id: int
    kind: str
    label: str
    status: str
    detail: str | None = None
    started_at: float = field(default_factory=time.monotonic)
    finished_at: float | None = None

    @property
    def elapsed(self) -> float:
        """Seconds since this activity started (or until it finished)."""
        end = self.finished_at if self.finished_at is not None else time.monotonic()
        return max(0.0, end - self.started_at)


class ActivityTracker:
    """Thread-safe registry of live activities.

    The tracker keeps finished activities for audit/debug but ``list(live_only=True)``
    filters them out so the status bar never shows completed work.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._activities: dict[int, Activity] = {}
        self._next_id: int = 1

    def start(
        self,
        kind: str,
        label: str,
        detail: str | None = None,
        status: str = "running",
    ) -> int:
        """Register a new live activity and return its id."""
        with self._lock:
            act_id = self._next_id
            self._next_id += 1
            self._activities[act_id] = Activity(
                id=act_id,
                kind=kind,
                label=label,
                status=status,
                detail=detail,
            )
            return act_id

    def update(self, act_id: int, *, detail: str | None = None, status: str | None = None) -> None:
        """Update the detail/status of a live activity in place."""
        with self._lock:
            act = self._activities.get(act_id)
            if act is None:
                return
            if detail is not None:
                act.detail = detail
            if status is not None:
                act.status = status

    def finish(self, act_id: int, *, ok: bool = True) -> None:
        """Mark an activity finished; it drops out of the live view."""
        with self._lock:
            act = self._activities.get(act_id)
            if act is None:
                return
            act.status = "done" if ok else "failed"
            act.finished_at = time.monotonic()

    def list(self, live_only: bool = True) -> list[Activity]:
        """Return live (or all) activities, oldest first."""
        with self._lock:
            acts = list(self._activities.values())
        if live_only:
            acts = [a for a in acts if a.finished_at is None]
        acts.sort(key=lambda a: a.started_at)
        return acts

    def clear(self) -> None:
        """Remove all activities (used in tests and on full redraw)."""
        with self._lock:
            self._activities.clear()

    def count(self, live_only: bool = True) -> int:
        """Number of activities in the requested view."""
        return len(self.list(live_only=live_only))

    def get(self, act_id: int) -> Activity | None:
        """Return a single activity by id, or None."""
        with self._lock:
            return self._activities.get(act_id)


#: Process-wide singleton.  The status bar and any delegated work both reach it.
_TRACKER: ActivityTracker | None = None
_TRACKER_LOCK = threading.Lock()


def get_tracker() -> ActivityTracker:
    """Return the process-wide ActivityTracker singleton."""
    global _TRACKER
    if _TRACKER is None:
        with _TRACKER_LOCK:
            if _TRACKER is None:
                _TRACKER = ActivityTracker()
    return _TRACKER


__all__ = [
    "Activity",
    "ActivityTracker",
    "get_tracker",
]
