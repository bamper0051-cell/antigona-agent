"""Д33 — Task Visualization: preview, progress, approval, result cards.

Every card is a pure-presentation widget that renders task state from the
EventBus.  No business logic, no database, no Verifier access.

The *conversation* itself stays card-free — these cards live only in the TUI.
"""

from __future__ import annotations

from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Grid, Horizontal
from textual.reactive import reactive
from textual.widgets import DataTable, Static

from antigona.tui_control.app import status_color

__all__ = ["TaskCard", "TaskPanel"]

CARD_COLUMNS = ("ID", "Goal", "Status", "Steps", "Duration")


class TaskCard(Static):
    """A single task card showing preview, progress, approval, and result."""

    task_id: reactive[str] = reactive("")
    goal: reactive[str] = reactive("")
    status: reactive[str] = reactive("")
    progress: reactive[float] = reactive(0.0)
    steps_total: reactive[int] = reactive(0)
    steps_done: reactive[int] = reactive(0)
    requires_approval: reactive[bool] = reactive(False)
    result: reactive[str] = reactive("")

    def __init__(
        self,
        task_id: str = "",
        goal: str = "",
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.task_id = task_id
        self.goal = goal

    def on_mount(self) -> None:
        self._rebuild()

    def _rebuild(self) -> None:
        lines = [
            f"[bold]Task:[/] {self.task_id[:12] if self.task_id else '-'}",
            f"[bold]Goal:[/] {self.goal}",
            f"[bold]Status:[/] [{status_color(self.status)}]{self.status}[/]",
        ]
        if self.steps_total > 0:
            pct = int(100 * self.progress)
            bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
            lines.append(f"[bold]Progress:[/] {bar} {self.steps_done}/{self.steps_total}")
        if self.requires_approval:
            lines.append("[yellow]⚠  Requires approval[/]")
        if self.result:
            lines.append(f"[bold]Result:[/] {self.result[:60]}")
        self.update("\n".join(lines))


class TaskPanel(Static):
    """Task visualization panel — renders cards from EventBus state."""

    task_count: reactive[int] = reactive(0)
    running_count: reactive[int] = reactive(0)

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._cards: dict[str, TaskCard] = {}

    def compose(self) -> ComposeResult:
        yield Horizontal(
            Static("Tasks", classes="panel_title"),
            Static(id="task_stats", classes="panel_stats"),
        )
        yield Grid(id="task_cards_grid")
        yield DataTable(id="task_list_table", cursor_type="row")

    def on_mount(self) -> None:
        table = self.query_one("#task_list_table", DataTable)
        table.add_columns(*CARD_COLUMNS)
        self._rebuild_stats()

    def upsert_card(self, task_id: str, **fields: Any) -> TaskCard:
        """Create or update a task card.

        Called from EventBus handler — never from business logic.
        """
        grid = self.query_one("#task_cards_grid", Grid)
        if task_id in self._cards:
            card = self._cards[task_id]
        else:
            card = TaskCard(task_id=task_id, id=f"card-{task_id}")
            self._cards[task_id] = card
            grid.mount(card)

        for key, value in fields.items():
            if hasattr(card, key):
                setattr(card, key, value)

        card._rebuild()
        self._rebuild_table()
        self._rebuild_stats()
        return card

    def remove_card(self, task_id: str) -> None:
        card = self._cards.pop(task_id, None)
        if card is not None:
            card.remove()
        self._rebuild_table()
        self._rebuild_stats()

    def _rebuild_table(self) -> None:
        try:
            table = self.query_one("#task_list_table", DataTable)
        except Exception:
            return
        table.clear()
        for tid, card in self._cards.items():
            table.add_row(
                tid[:12],
                card.goal[:40],
                Text(card.status, style=status_color(card.status)),
                f"{card.steps_done}/{card.steps_total}",
                "-",
            )

    def _rebuild_stats(self) -> None:
        self.task_count = len(self._cards)
        self.running_count = sum(
            1 for c in self._cards.values() if c.status in ("running", "planning", "tool_executing")
        )
        try:
            stats = self.query_one("#task_stats", Static)
            stats.update(f"Total: {self.task_count}  Running: {self.running_count}")
        except Exception:
            pass
