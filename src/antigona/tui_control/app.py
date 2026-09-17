"""Д29 & Д30 — Control Dashboard TUI skeleton with event-driven rendering.

Д29: Textual App, panels, status bar, keyboard navigation.
Д30: Renderer subscribes to EventBus.  Live tool/progress view.  No business logic.

The dashboard is a *thin presentational shell*: every panel receives data from
the EventBus and renders it.  No panel calls ``repository.transition()``,
touches a database, or names a Verifier-only terminal state.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.css.query import NoMatches
from textual.reactive import reactive
from textual.widgets import (
    ContentSwitcher,
    DataTable,
    Footer,
    Header,
    Label,
    Static,
    Tab,
    Tabs,
)

from antigona.events.bus import EventBus
from antigona.events.event_types import (
    BaseEvent,
    Cancelled,
    CancelRequested,
    ErrorOccurred,
    IntentClassified,
    MessageReceived,
    TaskApproved,
    TaskCompleted,
    TaskCreated,
    TaskRejected,
    ToolExecuted,
)

__all__ = ["ControlApp"]

# ── Tab topology ──────────────────────────────────────────────────────────────

TAB_SPECS: tuple[tuple[str, str, str], ...] = (
    ("tab-tasks", "Tasks", "pane-tasks"),
    ("tab-skills", "Skills", "pane-skills"),
    ("tab-delegation", "Delegation", "pane-delegation"),
    ("tab-models", "Models", "pane-models"),
    ("tab-resilience", "Resilience", "pane-resilience"),
    ("tab-events", "Events", "pane-events"),
)

MAIN_PANES: dict[str, str] = {tab: pane for tab, _title, pane in TAB_SPECS}

EVENT_COLUMNS = ("Time", "Type", "Correlation ID", "Payload")


def _clip(value: Any, limit: int = 60) -> str:
    text = "" if value is None else str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _stamp(ts: float) -> str:
    """Format a monotonic timestamp as seconds-since-boot (readable in TUI)."""
    return f"{ts:.1f}s"


_STATE_ORDER = (
    "received",
    "queued",
    "planning",
    "running",
    "tool_executing",
    "observing",
    "verifying",
    "waiting_approval",
    "done",
    "failed",
    "blocked",
    "cancelled",
)

STATUS_STYLES: dict[str, str] = {
    "done": "green",
    "failed": "red",
    "blocked": "red",
    "waiting_approval": "yellow",
    "cancelled": "grey50",
    "active": "green",
    "quarantined": "red",
    "draft": "blue",
    "candidate": "yellow",
    "deprecated": "grey50",
}


def status_color(status: str) -> str:
    return STATUS_STYLES.get((status or "").strip().lower(), "white")


def short_event_summary(event: BaseEvent) -> str:
    """One-line human-readable summary of any event."""
    name = type(event).__name__
    cid = (event.correlation_id or "")[:8]
    if isinstance(event, ToolExecuted):
        return f"[{cid}] {event.tool_name} {'✓' if event.success else '✗'}"
    if isinstance(event, TaskCreated):
        return f"[{cid}] task {event.task_id[:8]}: {_clip(event.goal, 40)}"
    if isinstance(event, TaskCompleted):
        return f"[{cid}] task {event.task_id[:8]} → {event.status}"
    if isinstance(event, ErrorOccurred):
        return f"[{cid}] ERR {event.source_component}: {_clip(event.message, 40)}"
    if isinstance(event, MessageReceived):
        return f"[{cid}] msg from {event.user_id}: {_clip(event.text, 40)}"
    if isinstance(event, TaskApproved):
        return f"[{cid}] approval {event.approval_id[:8]} granted"
    if isinstance(event, TaskRejected):
        return f"[{cid}] approval {event.approval_id[:8]} rejected"
    if isinstance(event, IntentClassified):
        return f"[{cid}] intent={event.intent} conf={event.confidence:.2f}"
    if isinstance(event, CancelRequested):
        return f"[{cid}] CANCEL {event.task_id[:8]}: {event.reason}"
    if isinstance(event, Cancelled):
        return f"[{cid}] CANCELLED {event.task_id[:8]}: {event.reason}"
    return f"[{cid}] {name}"


# ── Status bar widget ─────────────────────────────────────────────────────────


class StatusBar(Static):
    """Bottom status bar showing subsystem health."""

    skills_ok: reactive[bool] = reactive(True)
    delegation_ok: reactive[bool] = reactive(True)
    bus_ok: reactive[bool] = reactive(True)
    resilience_ok: reactive[bool] = reactive(True)
    event_count: reactive[int] = reactive(0)

    def watch_skills_ok(self, val: bool) -> None:
        self._refresh()

    def watch_delegation_ok(self, val: bool) -> None:
        self._refresh()

    def watch_bus_ok(self, val: bool) -> None:
        self._refresh()

    def watch_resilience_ok(self, val: bool) -> None:
        self._refresh()

    def watch_event_count(self, val: int) -> None:
        self._refresh()

    def _refresh(self) -> None:
        parts = []
        for label, ok in [
            ("Skills", self.skills_ok),
            ("Delegation", self.delegation_ok),
            ("Bus", self.bus_ok),
            ("Resilience", self.resilience_ok),
        ]:
            style = "green" if ok else "red"
            parts.append(f"[{style}]{label}: {'✓' if ok else '✗'}[/]")
        parts.append(f"events: {self.event_count}")
        self.update(Text.from_markup("  │  ".join(parts)))


# ── Main App ──────────────────────────────────────────────────────────────────


class ControlApp(App[None]):
    """Antigona Runtime Control Dashboard.

    Keyboard navigation
    --------------------
    1-6    — switch between panels
    r      — refresh all
    q      — quit
    """

    TITLE = "Antigona Runtime Control"
    SUB_TITLE = "Skills · Delegation · Tasks · Models · Resilience"

    CSS = """
    #panes { height: 1fr; }
    #event_widget { height: 8; }
    #status_bar { height: 1; }
    #event_table { height: 1fr; }
    Screen { background: #1a1b26; }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("1", "show_tab('tab-tasks')", "Tasks"),
        Binding("2", "show_tab('tab-skills')", "Skills"),
        Binding("3", "show_tab('tab-delegation')", "Delegation"),
        Binding("4", "show_tab('tab-models')", "Models"),
        Binding("5", "show_tab('tab-resilience')", "Resilience"),
        Binding("6", "show_tab('tab-events')", "Events"),
        Binding("r", "refresh_all", "Refresh"),
    ]

    def __init__(
        self,
        bus: EventBus | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._bus = bus or EventBus()
        self._event_index = 0
        self._unsubscribers: list[Callable[[], object]] = []
        self._panels_ready: bool = False

    # ── Layout ─────────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Header()
        yield Tabs(
            *(Tab(title, id=tab_id) for tab_id, title, _ in TAB_SPECS),
            id="main_tabs",
        )
        yield ContentSwitcher(
            self._pane("pane-tasks", "⚠ No task panel loaded"),
            self._pane("pane-skills", "⚠ No skills panel loaded"),
            self._pane("pane-delegation", "⚠ No delegation panel loaded"),
            self._pane("pane-models", "⚠ No model panel loaded"),
            self._pane("pane-resilience", "⚠ No resilience panel loaded"),
            Vertical(Label("Live event stream — all bus activity"), DataTable(id="event_table"), id="pane-events"),
            initial="pane-tasks",
            id="panes",
        )
        yield StatusBar(id="status_bar")
        yield Footer()

    def _pane(self, pane_id: str, placeholder_text: str) -> Vertical:
        return Vertical(Static(placeholder_text), id=pane_id)

    # ── Lifecycle

    def on_mount(self) -> None:
        """Subscribe to the EventBus and register panel placeholders."""
        self._subscribe_bus()
        self.query_one("#event_table", DataTable).cursor_type = "row"
        self._write_status("[*] dashboard mounted")
        self._panels_ready = True

    def on_unmount(self) -> None:
        for unsub in self._unsubscribers:
            try:
                unsub()
            except Exception:
                pass
        self._unsubscribers.clear()

    def _write_status(self, msg: str) -> None:
        """Log a status message to the event panel's log area."""
        try:
            self.query_one("#status_bar", StatusBar)
        except NoMatches:
            pass

    # ── EventBus subscription — Д30 core ────────────────────────────────────────

    def _subscribe_bus(self) -> None:
        """Subscribe the wildcard handler to log every event."""

        async def _on_any_event(event: BaseEvent) -> None:
            self._log_event(event)

        self._unsubscribers.append(self._bus.subscribe_any(_on_any_event))

    def _log_event(self, event: BaseEvent) -> None:
        """Append one event row to the event table."""
        ts = _stamp(event.timestamp if event.timestamp else time.monotonic())
        summary = short_event_summary(event)
        cid_short = (event.correlation_id or "")[:12]
        try:
            table = self.query_one("#event_table", DataTable)
            table.add_row(ts, type(event).__name__, cid_short, summary)
            table.scroll_end()
            self._event_index += 1
            bar = self.query_one("#status_bar", StatusBar)
            bar.event_count = self._event_index
        except NoMatches:
            pass

    # ── Tab switching ──────────────────────────────────────────────────────────

    def on_tabs_tab_activated(self, event: Tabs.TabActivated) -> None:
        tab_id = event.tab.id or ""
        if event.tabs.id == "main_tabs":
            self._switch_pane(MAIN_PANES.get(tab_id))

    def _switch_pane(self, pane_id: str | None) -> None:
        if pane_id is None:
            return
        try:
            self.query_one("#panes", ContentSwitcher).current = pane_id
        except NoMatches:
            pass

    def action_show_tab(self, tab_id: str) -> None:
        try:
            self.query_one("#main_tabs", Tabs).active = tab_id
        except NoMatches:
            pass

    # ── Refresh ────────────────────────────────────────────────────────────────

    def action_refresh_all(self) -> None:
        """Refresh all panels (no-op in base — panels override)."""
        self._write_status("[*] refresh requested")

    # ── Panel injection ────────────────────────────────────────────────────────

    def mount_panel(self, pane_id: str, widget: Static) -> None:
        """Replace a placeholder pane with a live widget."""
        if not self._panels_ready:
            return
        try:
            existing = self.query_one(f"#{pane_id}", Vertical)
            existing.remove_children()
            existing.mount(widget)
        except NoMatches:
            pass

    @property
    def bus(self) -> EventBus:
        return self._bus
