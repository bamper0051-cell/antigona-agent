"""Д31 — Skills Panel: manifest, discovery, activation, permissions.

Pure-presentation panel that reads skill state from the EventBus and renders
it.  No repository handle, no database session.
"""

from __future__ import annotations

from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.reactive import reactive
from textual.widgets import DataTable, Static

from antigona.tui_control.app import status_color

__all__ = ["SkillsPanel"]

COLUMNS = ("Name", "Version", "Status", "Trust", "Risk", "Trigger")


def _skill_rows() -> list[tuple[str, str, str, str, str, str]]:
    """Return skill rows from the event bus state (stub for now).

    In production the EventBus provides a SkillStateSnapshot event that the
    panel subscribes to and caches locally.
    """
    return []


class SkillsPanel(Static):
    """Skills manifest, activation, and permissions view.

    Reactively updates when the EventBus delivers skill lifecycle events.
    """

    skill_count: reactive[int] = reactive(0)
    active_count: reactive[int] = reactive(0)
    quarantined_count: reactive[int] = reactive(0)

    def on_mount(self) -> None:
        """Build the table and subscribe to bus events."""
        self._table: DataTable[Any] = DataTable(id="skills_table", cursor_type="row")
        self._table.add_columns(*COLUMNS)
        self._rebuild()

    def compose(self) -> ComposeResult:
        yield Horizontal(
            Static("Skills Dashboard", classes="panel_title"),
            Static(id="skills_stats", classes="panel_stats"),
        )
        yield DataTable(id="skills_table", cursor_type="row")

    def _rebuild(self) -> None:
        """Re-read skill state and refresh the table."""
        rows = _skill_rows()
        try:
            table = self.query_one("#skills_table", DataTable)
        except Exception:
            table = self._table
        table.clear()
        for name, ver, sts, trust, risk, trigger in rows:
            table.add_row(
                name,
                ver,
                Text(sts, style=status_color(sts)),
                trust,
                risk,
                trigger,
            )
        self.skill_count = len(rows)

    def watch_skill_count(self, count: int) -> None:
        try:
            self.query_one("#skills_stats", Static).update(
                f"Total: {count} | "
                f"Active: {self.active_count} | "
                f"Quarantined: {self.quarantined_count}"
            )
        except Exception:
            pass

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Show detail view for a selected skill (stub — emits bus event)."""
        if event.row_key.value is None:
            return
        # In D31 full: publish SkillSelected via EventBus


def skills_main() -> SkillsPanel:
    return SkillsPanel()
