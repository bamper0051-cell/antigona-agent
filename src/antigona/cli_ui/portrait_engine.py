"""Portrait Engine for Antigona CLI UI.

Provides deterministic state machine, gaze tracking controller, and fixed-width
glyph rendering for the Antigona CLI header.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final


class PortraitState(StrEnum):
    IDLE = "IDLE"
    TRACKING_INPUT = "TRACKING_INPUT"
    LOOK_DOWN_LEFT = "LOOK_DOWN_LEFT"
    LOOK_DOWN_CENTER = "LOOK_DOWN_CENTER"
    LOOK_DOWN_RIGHT = "LOOK_DOWN_RIGHT"
    ACKNOWLEDGE = "ACKNOWLEDGE"
    THINKING = "THINKING"
    WORKING = "WORKING"
    WAITING = "WAITING"
    SPEAKING = "SPEAKING"
    SUCCESS = "SUCCESS"
    ERROR = "ERROR"
    SLEEPING = "SLEEPING"


ACTIVE_AGENT_STATES: Final[set[PortraitState]] = {
    PortraitState.THINKING,
    PortraitState.WORKING,
    PortraitState.WAITING,
    PortraitState.SPEAKING,
}

_PANEL_STATUS_MAP: Final[dict[str, PortraitState]] = {
    "idle": PortraitState.IDLE,
    "sending": PortraitState.THINKING,
    "planning": PortraitState.THINKING,
    "tool_executing": PortraitState.WORKING,
    "observing": PortraitState.WORKING,
    "verifying": PortraitState.WAITING,
    "waiting_approval": PortraitState.WAITING,
    "done": PortraitState.SUCCESS,
    "failed": PortraitState.ERROR,
    "timeout": PortraitState.ERROR,
    "cancelled": PortraitState.ERROR,
    "reconnecting": PortraitState.ERROR,
}


#: Portrait *modes*, most important first.  A mode is coarser than a
#: ``PortraitState``: it answers "what is the portrait allowed to react to right
#: now" when several signals are live at once (the agent is running a tool while
#: the operator keeps typing).  Real agent work always outranks input tracking.
PORTRAIT_MODE_PRIORITY: Final[tuple[str, ...]] = ("working", "monitoring", "typing", "idle")

#: Statuses where Antigona is actively producing something.
WORKING_STATUSES: Final[frozenset[str]] = frozenset(
    {"sending", "planning", "running", "tool_executing"}
)

#: Statuses where Antigona is watching/awaiting rather than producing.
MONITORING_STATUSES: Final[frozenset[str]] = frozenset(
    {"observing", "verifying", "reconnecting", "waiting_approval"}
)


def resolve_portrait_mode(*, working: bool, monitoring: bool, typing: bool) -> str:
    """Collapse concurrent portrait signals into one mode by fixed priority."""
    if working:
        return "working"
    if monitoring:
        return "monitoring"
    if typing:
        return "typing"
    return "idle"


def portrait_mode_for_status(status: str, *, typing: bool = False) -> str:
    """Resolve the portrait mode for a panel status plus live input activity."""
    return resolve_portrait_mode(
        working=status in WORKING_STATUSES,
        monitoring=status in MONITORING_STATUSES,
        typing=typing,
    )


def portrait_state_for_status(panel_status: str) -> PortraitState:
    """Map panel status string to a target PortraitState."""
    return _PANEL_STATUS_MAP.get(panel_status, PortraitState.IDLE)


def gaze_state(value: str, cursor_position: int) -> PortraitState:
    """Map cursor position within the input buffer to Antigona's downward gaze.

    Empty input returns IDLE. Cursor position is clamped so adapters can safely
    pass positions from any input control.
    """
    if not value:
        return PortraitState.IDLE

    length = max(len(value), 1)
    cursor = max(0, min(cursor_position, length))
    ratio = cursor / length

    if ratio < 1 / 3:
        return PortraitState.LOOK_DOWN_LEFT
    if ratio <= 2 / 3:
        return PortraitState.LOOK_DOWN_CENTER
    return PortraitState.LOOK_DOWN_RIGHT


@dataclass(slots=True)
class PortraitController:
    """Pure state controller managing PortraitState transitions."""

    state: PortraitState = PortraitState.IDLE

    def on_typing(self, value: str, cursor_position: int) -> PortraitState:
        # Real active agent states take precedence over input gaze tracking
        if self.state in ACTIVE_AGENT_STATES:
            return self.state
        self.state = gaze_state(value, cursor_position)
        return self.state

    def on_submit(self) -> PortraitState:
        self.state = PortraitState.ACKNOWLEDGE
        return self.state

    def set_state(self, new_state: PortraitState) -> PortraitState:
        self.state = new_state
        return self.state

    def force_idle(self) -> PortraitState:
        self.state = PortraitState.IDLE
        return self.state

    def resolve_mode(self, panel_status: str, *, typing: bool = False) -> str:
        """Current portrait mode for a panel status (working > … > idle)."""
        return portrait_mode_for_status(panel_status, typing=typing)


_GLYPHS: Final[dict[PortraitState, str]] = {
    PortraitState.IDLE: "[◉ ◉]",
    PortraitState.TRACKING_INPUT: "[◉ ◉]",
    PortraitState.LOOK_DOWN_LEFT: "[◓ ◉]",
    PortraitState.LOOK_DOWN_CENTER: "[◉ ◉]",
    PortraitState.LOOK_DOWN_RIGHT: "[◉ ◓]",
    PortraitState.ACKNOWLEDGE: "[◕ ◕]",
    PortraitState.THINKING: "[◎ ◎]",
    PortraitState.WORKING: "[⚙ ⚙]",
    PortraitState.WAITING: "[◎ ◎]",
    PortraitState.SPEAKING: "[✦ ✦]",
    PortraitState.SUCCESS: "[✓ ✓]",
    PortraitState.ERROR: "[✖ ✖]",
    PortraitState.SLEEPING: "[- -]",
}


def get_portrait_glyph(state: PortraitState) -> str:
    """Return a deterministic 5-cell wide glyph for the given PortraitState."""
    return _GLYPHS.get(state, "[◉ ◉]")
