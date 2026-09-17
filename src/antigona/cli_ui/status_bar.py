"""Persistent animated bottom-toolbar for the Antigona CLI chat.

Renders a live status line at the bottom of the terminal via prompt_toolkit's
``bottom_toolbar=`` mechanism.  The callable reads the process-wide
:class:`~antigona.cli_ui.activity.ActivityTracker` every refresh cycle (~100ms)
and returns an :class:`prompt_toolkit.formatted_text.HTML` status line:

  - ``✦ idle`` (muted) when no activity is running;
  - one spinner + icon + label + detail + elapsed per live activity otherwise.

The toolbar is only visible while the prompt is accepting input (between turns).
During an in-flight turn Rich ``Live`` controls the terminal; the two rendering
systems never overlap, so this stays scroll-safe and conflict-free.

Aesthetic conventions (AURORA palette, mirrors the standalone Antigona CLI):
  - spinner frames ``⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏`` at ~10 FPS;
  - kind icons/colours: ``process`` ⚙️ violet ``#7C3AED``, ``subagent`` 🧠
    magenta ``#E83EDC``, fallback ● mint ``#56E2A0``;
  - idle / elapsed / separators in muted greys.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Final

from prompt_toolkit.formatted_text import HTML

from antigona.cli_ui.activity import Activity, get_tracker

if TYPE_CHECKING:
    from antigona.cli_ui.models import ChatUIState

#: Braille spinner frames (10), ~10 FPS via ``int(time.monotonic()*10) % 10``.
_SPINNER: Final[str] = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

#: Per-kind presentation.
KIND_ICONS: Final[dict[str, str]] = {"process": "⚙️", "subagent": "🧠"}
KIND_COLORS: Final[dict[str, str]] = {"process": "#7C3AED", "subagent": "#E83EDC"}

#: Muted palette (idle, elapsed, separators, overflow).
_MUTED_GREY: Final[str] = "#5A5878"
_MUTED_LAVENDER: Final[str] = "#7C7A96"
_FALLBACK_COLOR: Final[str] = "#56E2A0"

#: Layout limits.
_MAX_VISIBLE: Final[int] = 3
_MAX_LABEL: Final[int] = 40
_MAX_DETAIL: Final[int] = 30


def _truncate(text: str, max_width: int) -> str:
    """Truncate text to *max_width* chars, appending ``…`` when cut."""
    text = text.strip()
    if len(text) <= max_width:
        return text
    return text[: max_width - 1] + "…"


def _elapsed_label(elapsed: float) -> str:
    """Format an elapsed time (seconds) as a compact human label."""
    secs = int(elapsed)
    if secs < 60:
        return f"{secs}s"
    mins, rem = divmod(secs, 60)
    if mins < 60:
        return f"{mins}m {rem:02d}s"
    hours, min_rem = divmod(mins, 60)
    return f"{hours}h {min_rem:02d}m"


def _build_one(activity: Activity, spinner: str) -> str:
    """Render a single activity as an HTML fragment."""
    icon = KIND_ICONS.get(activity.kind, "●")
    colour = KIND_COLORS.get(activity.kind, _FALLBACK_COLOR)
    label = _truncate(activity.label, _MAX_LABEL)
    parts: list[str] = [
        f'<style fg="{colour}">{spinner}</style>',
        f' <style fg="{colour}"><b>{icon} {label}</b></style>',
    ]
    if activity.detail:
        parts.append(
            f' <style fg="{_MUTED_LAVENDER}">· {_truncate(activity.detail, _MAX_DETAIL)}</style>'
        )
    parts.append(f' <style fg="{_MUTED_GREY}">{_elapsed_label(activity.elapsed)}</style>')
    return "".join(parts)


def build_status_bar() -> HTML:
    """Return the HTML status line for the bottom-toolbar.

    Reads the live activities from the process-wide tracker.  Returns ``✦ idle``
    in muted grey when nothing is running, else one animated entry per activity
    (max ``_MAX_VISIBLE`` inline, ``+N more`` for the rest).
    """
    active = get_tracker().list(live_only=True)
    if not active:
        return HTML(f'<style fg="{_MUTED_GREY}"> ✦ idle </style>')

    spinner = _SPINNER[int(time.monotonic() * 10) % len(_SPINNER)]
    tokens: list[str] = []
    for i, act in enumerate(active[:_MAX_VISIBLE]):
        if i > 0:
            tokens.append(f'<style fg="{_MUTED_LAVENDER}"> │ </style>')
        tokens.append(_build_one(act, spinner))
    if len(active) > _MAX_VISIBLE:
        tokens.append(
            f' <style fg="{_MUTED_LAVENDER}">+{len(active) - _MAX_VISIBLE} more</style>'
        )
    return HTML("".join(tokens))


def _render_progress_bar_html(color: str = "#7C3AED") -> str:
    """Render an animated 12-cell progress bar + percentage for active tasks."""
    now = time.monotonic()
    phase = (now * 2.0) % 10.0
    percent = max(0.1, min(0.95, phase / 10.0))
    filled = int(percent * 10)
    bar = "█" * filled + "░" * (10 - filled)
    pct_text = f"{int(percent * 100):2d}%"
    return f'<style fg="{color}"><b>[{bar}] {pct_text}</b></style>'


def build_pipeline_bar(state: ChatUIState | None = None) -> HTML:
    """Live process pipeline for the bottom toolbar (spec §8).

    Mirrors the agent's visible process — phase + progress bar + real EventLog
    events — next to the input line, plus active trackers.
    """
    from antigona.cli_ui.panel import EVENT_EMOJI, STATE_META

    tokens: list[str] = []

    if state is not None and state.current_status:
        _, _, color, label = STATE_META.get(state.current_status, ("⚪", "[--]", _MUTED_LAVENDER, state.current_status))
        tokens.append(f'<style fg="{color}"><b>{label}</b></style>')

        if state.current_status in {"sending", "planning", "tool_executing", "observing", "verifying", "reconnecting"}:
            tokens.append(_render_progress_bar_html(color))

    if state is not None:
        for event in state.events[-3:]:
            kind = ""
            detail = ""
            if isinstance(event, dict):
                kind = str(event.get("event") or "")
                detail = str(event.get("detail") or "")
            icon = EVENT_EMOJI.get(kind, "•")
            if detail:
                tokens.append(
                    f' <style fg="{_MUTED_LAVENDER}">· {icon} {_truncate(detail, _MAX_DETAIL)}</style>'
                )

    tracker = get_tracker()
    active = tracker.list(live_only=True)
    if active:
        spinner = _SPINNER[int(time.monotonic() * 10) % len(_SPINNER)]
        parts: list[str] = []
        for act in active[: _MAX_VISIBLE]:
            icon = KIND_ICONS.get(act.kind, "●")
            color = KIND_COLORS.get(act.kind, _FALLBACK_COLOR)
            label = _truncate(act.label, _MAX_LABEL)
            parts.append(f'<style fg="{color}">{icon} {label}</style>')
        tokens.append(f' <style fg="{_MUTED_GREY}">{spinner}</style> ' + "  ".join(parts))
    elif state is None or not state.current_status or state.current_status in {"idle", "done"}:
        tokens.append(f'<style fg="{_MUTED_GREY}">✦ idle</style>')

    return HTML("  ".join(tokens) if tokens else "<style> </style>")


__all__ = [
    "KIND_COLORS",
    "KIND_ICONS",
    "build_pipeline_bar",
    "build_status_bar",
]
