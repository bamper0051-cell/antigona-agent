"""TUI Control Dashboard — internal Antigona runtime control panel.

A Textual-based dashboard that gives operators live visibility into the
Antigona runtime: skills, delegation contracts, running tasks, model/provider
selection, and resilience status.  Every subsystem is visualised through
*pure-presentation* panels that hold no business logic — all state transitions
go through the EventBus.

Panels
------
SkillsPanel       D31  — manifest, discovery, activation, permissions
DelegationPanel   D32  — Claude / Codex / Antigravity adapters, artifact flow
TaskPanel         D33  — preview / progress / approval / result cards
ModelPanel        D34  — provider/model capabilities & /setllm UI
ResiliencePanel   D35  — retry budgets, circuit breakers, queue depth

No panel touches a repository, a database, or the Verifier.
"""

from __future__ import annotations

from .app import ControlApp
from .delegation_panel import DelegationPanel
from .model_panel import ModelPanel
from .resilience import ResiliencePanel
from .skills_panel import SkillsPanel
from .task_panel import TaskPanel

__all__ = [
    "ControlApp",
    "DelegationPanel",
    "ModelPanel",
    "ResiliencePanel",
    "SkillsPanel",
    "TaskPanel",
]


def main() -> None:
    """Entry point: ``antigona-tui-control``."""
    app = ControlApp()
    app.run()
