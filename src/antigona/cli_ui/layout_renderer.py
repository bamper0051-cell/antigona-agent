"""Renderer adapter that routes all presentation into the single full-screen layout.

When the CLI runs through ``AntigonaLayout`` (prompt_toolkit owns the alternate
screen), the ChatController must never write to stdout with Rich — that is the
historical cause of flicker, cursor jumps and CPR artifacts in Termux/SSH.
This adapter implements the same surface as ``CliRenderer`` (see
``RendererProtocol`` in ``chat.py``) but only requests repaints on the running
application: messages, outcomes and the panel already live in ``ChatUIState``
and the layout draws them from state.

No parallel UI is introduced — this is a routing shim to the one layout, and
it degrades to a no-op before the layout exists (initial panel/monitor setup).
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from rich.spinner import Spinner

    from antigona.cli_ui.layout import AntigonaLayout


class LayoutRendererAdapter:
    """Routes ChatController presentation calls to the full-screen layout."""

    def __init__(self) -> None:
        self._layout: AntigonaLayout | None = None

    def attach(self, layout: AntigonaLayout) -> None:
        """Bind the layout instance (called once it exists)."""
        self._layout = layout

    def _repaint(self) -> None:
        if self._layout is not None:
            self._layout.request_repaint()

    # ── RendererProtocol surface (mirrors CliRenderer) ───────────────────────
    # Nothing writes to stdout: every call only schedules a repaint of the one
    # layout, which re-reads ChatUIState.

    def render_message(self, role: Any, content: str, width: int | None = None) -> None:
        self._repaint()

    def render_state(self, state: Any) -> None:
        self._repaint()

    def render_outcome(self, outcome: Any) -> None:
        self._repaint()

    def render_panel(self, state: Any) -> None:
        self._repaint()

    def update_panel(self, state: Any) -> None:
        self._repaint()

    def clear_terminal(self) -> None:
        # The layout owns the screen; clearing here would fight it.
        pass

    def clear_and_repaint(self, state: Any) -> None:
        self._repaint()

    @contextmanager
    def live_spinner(
        self,
        status_text: str = "Processing...",
        spinner_name: str = "dots",
    ) -> Generator[Spinner | None, None, None]:
        # The layout's own status bar shows the spinner; Rich Live is never used.
        yield None

    def flush(self) -> None:
        pass

    def release(self) -> None:
        pass


__all__ = ["LayoutRendererAdapter"]
