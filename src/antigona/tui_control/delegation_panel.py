"""Д32 — Delegation Panel: real-time adapter status and artifact flow.

Reads adapter state from the EventBus and renders it.
No business logic — pure presentation.
"""

from __future__ import annotations

from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.reactive import reactive
from textual.widgets import DataTable, Label, Static

from antigona.tui_control.app import status_color
from antigona.tui_control.delegation import (
    AdapterStatus,
    AntigravityAdapter,
    ClaudeAdapter,
    CodexAdapter,
    Delegate,
)

__all__ = ["DelegationPanel"]

ADAPTER_COLUMNS = ("Adapter", "Status", "Calls", "Last Duration", "Last Error")


class DelegationPanel(Static):
    """Delegation adapters dashboard — Claude, Codex, Antigravity.

    Each adapter appears as a row in the table with current status,
    call count, and last run duration.  Artifacts returned by adapters
    are listed in a secondary table.
    """

    claude: ClaudeAdapter
    codex: CodexAdapter
    antigravity: AntigravityAdapter
    total_calls: reactive[int] = reactive(0)
    failed_calls: reactive[int] = reactive(0)

    def __init__(
        self,
        claude: ClaudeAdapter | None = None,
        codex: CodexAdapter | None = None,
        antigravity: AntigravityAdapter | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.claude = claude or ClaudeAdapter()
        self.codex = codex or CodexAdapter()
        self.antigravity = antigravity or AntigravityAdapter()
        self._adapters: list[Delegate] = [self.claude, self.codex, self.antigravity]
        self._call_history: dict[str, int] = {a.name: 0 for a in self._adapters}
        self._last_duration: dict[str, str] = {a.name: "-" for a in self._adapters}
        self._last_error: dict[str, str] = {a.name: "" for a in self._adapters}

    def on_mount(self) -> None:
        self._table = self.query_one("#adapter_table", DataTable)
        self._table.add_columns(*ADAPTER_COLUMNS)
        self._rebuild()

    def compose(self) -> ComposeResult:
        yield Horizontal(
            Static("Delegation Adapters", classes="panel_title"),
            Static(id="delegation_stats", classes="panel_stats"),
        )
        yield DataTable(id="adapter_table", cursor_type="row")
        yield Label("Artifact flow — returned by last call:")
        yield DataTable(id="artifact_table", cursor_type="row")

    def _rebuild(self) -> None:
        try:
            table = self.query_one("#adapter_table", DataTable)
        except Exception:
            table = self._table
        table.clear()
        for adapter in self._adapters:
            name = adapter.name
            sts = getattr(adapter, "status", AdapterStatus.IDLE)
            if isinstance(sts, AdapterStatus):
                sts = sts.value
            table.add_row(
                name,
                Text(sts, style=status_color(sts)),
                str(self._call_history[name]),
                self._last_duration[name],
                self._last_error[name] or "-",
            )

    def record_call(self, adapter_name: str, duration: float, error: str = "") -> None:
        """Record a delegation call result (called from EventBus handler)."""
        self._call_history[adapter_name] = self._call_history.get(adapter_name, 0) + 1
        self._last_duration[adapter_name] = f"{duration:.1f}s"
        self._last_error[adapter_name] = error
        self.total_calls = sum(self._call_history.values())
        self.failed_calls = sum(1 for e in self._last_error.values() if e)
        self._rebuild()
