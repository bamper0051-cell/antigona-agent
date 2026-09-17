"""Pure CLI UI presentation layer renderer.

Clean-room implementation of Rich presentation rendering without Core/Gateway,
network, database, or process dependencies.
"""

import os
import sys
from collections.abc import Generator, Hashable, Mapping
from contextlib import contextmanager
from typing import IO, Any, Literal

from rich.cells import cell_len
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.spinner import Spinner
from rich.text import Text

from antigona.cli_ui.models import (
    ChatMessageRole,
    ChatUIState,
    TerminalOutcome,
    TerminalOutcomeStatus,
)

#: Message-metadata keys beginning with this prefix are presentation-insignificant
#: bookkeeping (transport ids, retry counters, render timestamps).  They are kept
#: out of the render key so they cannot force redundant frames; every other key is
#: significant and a change to it re-renders.
INSIGNIFICANT_METADATA_PREFIX = "_"


def _normalize(value: Any) -> Hashable:
    """Collapse an arbitrary value into a deterministic, immutable, hashable form.

    The type name is carried alongside the value so that look-alikes (``1`` vs
    ``True``, a raw string vs an enum member) never collapse onto one key.
    Values that cannot be compared structurally degrade to their ``repr``, which
    is conservative: an unrecognised payload re-renders instead of being dropped.
    """
    if value is None or isinstance(value, str | bool | int | float):
        return (type(value).__name__, value)
    if isinstance(value, Mapping):
        return (
            "map",
            tuple(
                sorted(
                    ((str(key), _normalize(item)) for key, item in value.items()),
                    key=lambda pair: pair[0],
                )
            ),
        )
    if isinstance(value, list | tuple):
        return ("seq", tuple(_normalize(item) for item in value))
    if isinstance(value, set | frozenset):
        return ("set", tuple(sorted(repr(_normalize(item)) for item in value)))
    return ("opaque", type(value).__name__, repr(value))


def _significant_metadata(metadata: Mapping[str, Any] | None) -> Hashable:
    """Return the deterministic key fragment for presentation-significant metadata."""
    if not metadata:
        return ()
    return tuple(
        sorted(
            (
                (str(key), _normalize(value))
                for key, value in metadata.items()
                if not str(key).startswith(INSIGNIFICANT_METADATA_PREFIX)
            ),
            key=lambda pair: pair[0],
        )
    )


def compute_render_key(state: ChatUIState) -> tuple[Hashable, ...]:
    """Build the deterministic immutable identity of one presented UI frame.

    Two states share a key only when everything the renderer would draw is the
    same: the last message (role, content, timestamp, significant metadata) plus
    how many messages precede it, the status line, the animation flag and spinner,
    the terminal outcome (typed status, result data, error message), the target
    width, and the colour mode.  Anything outside that tuple cannot suppress a
    frame, and anything inside it re-renders when it changes.
    """
    messages = state.messages
    if messages:
        last = messages[-1]
        role = last.role.value if isinstance(last.role, ChatMessageRole) else str(last.role)
        message_key: tuple[Hashable, ...] = (
            "message",
            len(messages),
            type(last.role).__name__,
            role,
            last.content,
            last.timestamp,
            _significant_metadata(last.metadata),
        )
    else:
        message_key = ("no-message", 0)

    outcome = state.terminal_outcome
    if outcome is None:
        outcome_key: tuple[Hashable, ...] = ("no-outcome",)
    else:
        status = outcome.status
        status_value = status.value if isinstance(status, TerminalOutcomeStatus) else str(status)
        outcome_key = (
            "outcome",
            type(status).__name__,
            status_value,
            _normalize(outcome.result_data),
            outcome.error_message,
        )

    return (
        message_key,
        state.current_status,
        state.is_animating,
        state.spinner_name,
        outcome_key,
        state.width,
        state.no_color,
    )


def truncate_unicode(text: str, max_width: int) -> str:
    """Truncate text so that its visual cell length does not exceed max_width."""
    if max_width <= 0:
        return ""
    if cell_len(text) <= max_width:
        return text

    current_width = 0
    result: list[str] = []
    for char in text:
        w = cell_len(char)
        if current_width + w > max_width:
            break
        result.append(char)
        current_width += w
    return "".join(result)


class CliRenderer:
    """Pure terminal presentation layer built on Rich."""

    def __init__(
        self,
        file: IO[str] | None = None,
        no_color: bool | None = None,
        force_terminal: bool | None = None,
        width: int | None = None,
    ) -> None:
        if no_color is None:
            no_color = "NO_COLOR" in os.environ

        self._target_file = file or sys.stdout
        self._no_color = no_color

        if hasattr(self._target_file, "isatty"):
            try:
                self._is_tty = bool(self._target_file.isatty())
            except Exception:
                self._is_tty = False
        else:
            self._is_tty = False

        color_system: Literal["auto", "standard", "256", "truecolor", "windows"] | None = (
            None if no_color else "auto"
        )

        self.console = Console(
            file=self._target_file,
            no_color=no_color,
            color_system=color_system,
            force_terminal=force_terminal,
            width=width,
            highlight=False,
        )

        self._active_live: Live | None = None
        self._last_render_key: tuple[Hashable, ...] | None = None
        self._panel_drawn: bool = False

    @property
    def is_tty(self) -> bool:
        """Return True if target file stream is an interactive TTY."""
        return self._is_tty

    @property
    def animations_enabled(self) -> bool:
        """Animations are enabled only if the stream is interactive TTY and force_terminal is true."""
        return self.console.is_terminal and self._is_tty

    def render_message(
        self,
        role: ChatMessageRole | str,
        content: str,
        width: int | None = None,
    ) -> None:
        """Render a single chat message with appropriate role styling."""
        role_str = role.value if isinstance(role, ChatMessageRole) else str(role)
        target_width = width or self.console.width

        formatted_content = truncate_unicode(content, target_width)

        if self._no_color:
            self.console.print(f"[{role_str.upper()}] {formatted_content}")
            self.flush()
            return

        role_colors = {
            "user": "bold cyan",
            "assistant": "bold green",
            "system": "bold yellow",
            "tool": "bold magenta",
            "error": "bold red",
            "warning": "bold yellow",
            "info": "bold blue",
        }
        style = role_colors.get(role_str.lower(), "bold white")

        role_title = Text(f"[{role_str.upper()}]", style=style)
        self.console.print(role_title, Text(formatted_content))
        self.flush()

    def render_notification(self, level: str, message: str) -> None:
        """Render an explicit notification message (error, warning, info)."""
        level_clean = level.lower()
        if self._no_color:
            self.console.print(f"[{level_clean.upper()}] {message}")
            self.flush()
            return

        styles = {
            "error": "bold red",
            "warning": "bold yellow",
            "info": "bold blue",
        }
        style = styles.get(level_clean, "bold white")
        panel = Panel(
            Text(message, style=style),
            title=f"Notification: {level_clean.upper()}",
            border_style=style,
            expand=False,
        )
        self.console.print(panel)
        self.flush()

    def render_outcome(self, outcome: TerminalOutcome) -> None:
        """Render a typed terminal outcome gate.

        Only ``outcome.is_success()`` opens the success panel.  For every other
        outcome the title is built from the *typed* status alone — an untyped
        status is reported as ``MALFORMED`` and its text is never promoted into
        the title, so a wire value spelling a success token cannot dress a failure
        up as a success banner.  The unvalidated text is still shown in the body,
        explicitly labelled, so the failure stays diagnosable.
        """
        if outcome.is_success():
            title = "TERMINAL OUTCOME: SUCCESS"
            style = "bold green" if not self._no_color else ""
            summary = str(outcome.result_data) if outcome.result_data is not None else "Operation completed successfully."
            body = f"{summary}"
        else:
            style = "bold red" if not self._no_color else ""
            err_msg = outcome.error_message or "Terminal outcome was not successful."
            if isinstance(outcome.status, TerminalOutcomeStatus):
                typed_status = outcome.status.value
                title = f"TERMINAL OUTCOME: {typed_status}"
                body = f"Status: {typed_status}\nDetails: {err_msg}"
            else:
                typed_status = TerminalOutcomeStatus.MALFORMED.value
                reported = truncate_unicode(str(outcome.status), 120)
                title = f"TERMINAL OUTCOME: {typed_status}"
                body = (
                    f"Status: {typed_status}\n"
                    f"Reported (unvalidated): {reported}\n"
                    f"Details: {err_msg}"
                )

        if self._no_color:
            self.console.print(f"=== {title} ===")
            self.console.print(body)
        else:
            panel = Panel(
                Text(body),
                title=Text(title, style=style),
                border_style=style,
                expand=False,
            )
            self.console.print(panel)
        self.flush()

    def render_panel(self, state: ChatUIState) -> None:
        """Render the static banner + status panel (drawn once at session start).

        Safe on both TTY and non-TTY streams: uses the same console as the rest
        of the renderer, so the panel appears above the chat history.  The panel
        is intentionally *not* animated — it is redrawn only when the underlying
        state changes (see :meth:`update_panel`).
        """
        from antigona.cli_ui.panel import render_panel as _render_panel

        _render_panel(self.console, state)
        self._panel_drawn = True
        self.flush()

    def clear_terminal(self) -> None:
        """Move the cursor to the top-left and clear the screen (TTY only)."""
        if self.animations_enabled:
            self.console.file.write("\x1b[2J\x1b[H")
            self.console.file.flush()

    def clear_and_repaint(self, state: ChatUIState) -> None:
        """Clear the terminal and redraw the static banner + panel (``/clear``).

        On non-TTY streams this only redraws the panel below the transcript.
        """
        self.clear_terminal()
        from antigona.cli_ui.panel import render_panel as _render_panel

        _render_panel(self.console, state)
        self._panel_drawn = True
        self.flush()

    def update_panel(self, state: ChatUIState) -> None:
        """Redraw the static panel in place after state changes.

        On interactive TTY sessions the panel is drawn once at session start
        and never moved: jumping the cursor over the panel block fights the
        prompt-toolkit renderer (the panel would reappear mid-screen and the
        input line would be duplicated).  Live process state is reflected in
        the prompt-toolkit bottom toolbar instead.  On non-TTY streams the
        panel is simply reprinted below the transcript.
        """
        from antigona.cli_ui.panel import render_panel as _render_panel

        if self.animations_enabled and self._panel_drawn:
            return  # TTY: panel is static; live updates live in the toolbar
        _render_panel(self.console, state)
        self._panel_drawn = True
        self.flush()

    def render_state(self, state: ChatUIState) -> None:
        """Render UI state, suppressing a write only for an unchanged frame.

        Deduplication compares the full :func:`compute_render_key` tuple, not a
        hash of the message count, so an edited message, a changed role,
        timestamp or significant metadata, and a changed outcome payload all
        re-render.
        """
        render_key = compute_render_key(state)

        if self._last_render_key == render_key:
            return

        self._last_render_key = render_key

        if state.messages:
            last_msg = state.messages[-1]
            self.render_message(last_msg.role, last_msg.content, width=state.width)

        if state.terminal_outcome:
            self.render_outcome(state.terminal_outcome)

    @contextmanager
    def live_spinner(
        self,
        status_text: str = "Processing...",
        spinner_name: str = "dots",
    ) -> Generator[Spinner | None, None, None]:
        """Bounded animation context manager with Ctrl-safe cleanup.

        Disables live animation on non-TTY streams.  The spinner is transient
        (never leaves garbage on screen) and stops cleanly on DONE / error /
        cancellation — it never runs a full-screen interface.
        """
        if not self.animations_enabled:
            if not self._no_color:
                self.console.print(f"... {status_text}")
                self.flush()
            yield None
            return

        spinner = Spinner(spinner_name, text=status_text)
        live = Live(
            spinner,
            console=self.console,
            refresh_per_second=10,
            transient=True,
        )
        self._active_live = live
        try:
            live.start()
            yield spinner
        finally:
            try:
                live.stop()
            except Exception:
                pass
            self._active_live = None
            self.flush()

    def flush(self) -> None:
        """Flush the console file stream."""
        try:
            if hasattr(self._target_file, "flush"):
                self._target_file.flush()
        except Exception:
            pass

    def release(self) -> None:
        """Cleanly release terminal presentation state and active displays."""
        if self._active_live is not None:
            try:
                self._active_live.stop()
            except Exception:
                pass
            self._active_live = None
        self.flush()
