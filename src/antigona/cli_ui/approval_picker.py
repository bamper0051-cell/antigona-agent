"""Interactive CLI approval picker — no internal IDs exposed to the user.

When the Gateway reports WAITING_APPROVAL the CLI automatically surfaces
an interactive picker that lets the owner approve, deny, or inspect
pending decisions without ever seeing or typing flow_id / approval_id.

The picker stores the selected item internally and passes the real
identifier to the Gateway on the owner's behalf.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from antigona.cli_ui.models import ChatMessageRole


class PickerAction(StrEnum):
    APPROVE = "approve"
    DENY = "deny"
    DETAILS = "details"
    CANCEL = "cancel"


@dataclass(frozen=True)
class ApprovalEntry:
    """A single pending approval presented to the user.

    The internal ``approval_id`` is never shown; the user sees only
    the safe display fields (tool, risk, reason).
    """

    approval_id: str
    tool_name: str
    risk_level: str
    reason: str
    flow_id: str
    display_label: str = ""


@dataclass
class PickerState:
    """Mutable state of the interactive approval picker."""

    entries: list[ApprovalEntry] = field(default_factory=list)
    selected_index: int = 0
    mode: str = "single"  # "single" | "multi"
    detail_view: bool = False
    closed: bool = False
    result: PickerAction | None = None
    chosen_entry: ApprovalEntry | None = None


def _short_id(value: str, keep: int = 8) -> str:
    if len(value) <= keep + 1:
        return value
    return f"{value[:keep]}…"


def _risk_color(risk: str) -> str:
    return {"LOW": "green", "MEDIUM": "yellow", "HIGH": "red"}.get(
        risk.upper(), "white"
    )


def build_picker_entries(
    approvals: list[Any],
    gateway: Any,
) -> list[ApprovalEntry]:
    """Convert raw Gateway approval objects into user-safe entries."""
    entries: list[ApprovalEntry] = []
    for app in approvals:
        if isinstance(app, dict):
            aid = str(app.get("id", ""))
            tool = str(app.get("tool_name", ""))
            risk = str(app.get("risk_level", ""))
            reason = str(app.get("reason", ""))
            task_id = str(app.get("task_id", ""))
        else:
            aid = str(getattr(app, "id", ""))
            tool = str(getattr(app, "tool_name", ""))
            risk = str(getattr(app, "risk_level", ""))
            reason = str(getattr(app, "reason", ""))
            task_id = str(getattr(app, "task_id", ""))
        if not aid:
            continue
        display_label = f"{tool} — {reason[:60]}"
        entries.append(
            ApprovalEntry(
                approval_id=aid,
                tool_name=tool,
                risk_level=risk,
                reason=reason,
                flow_id=task_id,
                display_label=display_label,
            )
        )
    return entries


async def show_single_approval_picker(
    gateway: Any,
    renderer: Any,
    approval: ApprovalEntry,
    key_bridge: Any = None,
) -> tuple[PickerAction | None, ApprovalEntry | None]:
    """Show a single approval with full detail and action keys.

    Returns ``(action, chosen_entry)``; ``chosen_entry`` is set when the user
    confirmed a decision, ``None`` on cancel.
    """
    state = PickerState(entries=[approval], mode="single")
    action = await _run_picker_loop(gateway, renderer, state, key_bridge=key_bridge)
    return action, state.chosen_entry


async def show_multi_approval_picker(
    gateway: Any,
    renderer: Any,
    approvals: list[ApprovalEntry],
    key_bridge: Any = None,
) -> tuple[PickerAction | None, ApprovalEntry | None]:
    """Show a compact list of approvals with arrow navigation.

    Returns ``(action, chosen_entry)``; ``chosen_entry`` is set when the user
    confirmed a decision, ``None`` on cancel.
    """
    state = PickerState(entries=approvals, mode="multi")
    action = await _run_picker_loop(gateway, renderer, state, key_bridge=key_bridge)
    return action, state.chosen_entry


async def _run_picker_loop(
    gateway: Any,
    renderer: Any,
    state: PickerState,
    key_bridge: Any = None,
) -> PickerAction:
    """Main picker event loop — reads keypresses and dispatches actions.

    Two input modes, one picker:
    * Full-screen layout mode (``key_bridge`` active): keys arrive through the
      layout's key bindings.  A nested PromptSession here would fight the
      full-screen Application (alternate screen, CPR probes) — the layout path
      must never open one.
    * Plain prompt mode (``run_interactive_loop``): the legacy PromptSession
      path, unchanged.
    """
    if key_bridge is not None and key_bridge.active:
        while not state.closed:
            _render_picker(renderer, state)
            key = await key_bridge.next_key()
            if _apply_picker_key(key, state):
                break
        return state.result or PickerAction.CANCEL

    from prompt_toolkit import PromptSession
    from prompt_toolkit.history import DummyHistory

    session: PromptSession[str] = PromptSession(history=DummyHistory())
    while not state.closed:
        _render_picker(renderer, state)

        key = await session.prompt_async(
            "\n[dim]←/→ navigate · Enter select · Y approve · N deny · D details · Esc close[/dim]\n> ",
            multiline=False,
        )
        if not key or not key.strip():
            continue
        if _apply_picker_key(key.strip(), state):
            break
    return state.result or PickerAction.CANCEL


def _apply_picker_key(key: str, state: PickerState) -> bool:
    """Apply one picker key; returns True once the picker has closed."""
    action = _map_key_to_action(key, state)
    if action is None:
        return False
    if action is PickerAction.CANCEL:
        state.closed = True
        state.result = PickerAction.CANCEL
        return True
    if action in (PickerAction.APPROVE, PickerAction.DENY):
        state.result = action
        state.chosen_entry = state.entries[state.selected_index]
        state.closed = True
        return True
    if action is PickerAction.DETAILS:
        state.detail_view = not state.detail_view
        return False
    return False


def _map_key_to_action(key: str, state: PickerState) -> PickerAction | None:
    if key in ("\x1b[A", "\x1b[B", "up", "down"):
        _move_selection(state, 1 if key in ("\x1b[B", "down") else -1)
        return None
    if key in ("\x1b[C", "\x1b[D", "left", "right"):
        _move_selection(state, 1 if key in ("\x1b[C", "right") else -1)
        return None
    if key in ("\r", "\n", "enter"):
        return PickerAction.APPROVE
    if key.lower() == "y":
        return PickerAction.APPROVE
    if key.lower() == "n":
        return PickerAction.DENY
    if key.lower() == "d":
        return PickerAction.DETAILS
    if key in ("\x1b", "escape"):
        return PickerAction.CANCEL
    return None


def _move_selection(state: PickerState, delta: int) -> None:
    if not state.entries:
        return
    state.selected_index = (state.selected_index + delta) % len(state.entries)


def _render_picker(renderer: Any, state: PickerState) -> None:
    """Render the picker UI to the terminal."""
    if not state.entries:
        renderer.render_message(ChatMessageRole.INFO, "Нет ожидающих подтверждений.")
        return
    lines: list[str] = []
    if state.mode == "multi":
        lines.append(f"🔐 Ожидают подтверждения: {len(state.entries)}")
        for idx, entry in enumerate(state.entries):
            marker = "> " if idx == state.selected_index else "  "
            risk_color = _risk_color(entry.risk_level)
            lines.append(f"{marker} [{risk_color}]{entry.risk_level}[/] {entry.display_label}")
        lines.append("↑↓ выбрать · Enter открыть · Y одобрить · N отклонить")
    else:
        entry = state.entries[0]
        lines.append("🔐 Требуется подтверждение")
        lines.append(f"🛠 {entry.tool_name}")
        lines.append(f"Риск: {entry.risk_level}")
        lines.append(f"Причина: {entry.reason}")
        lines.append("")
        lines.append("  [ ✅ Одобрить ]  [ ❌ Отклонить ]  [ 🔎 Подробнее ]")
    if state.detail_view and state.entries:
        entry = state.entries[state.selected_index]
        lines.append("")
        lines.append("── Подробная информация ──")
        lines.append(f"  Инструмент: {entry.tool_name}")
        lines.append(f"  Уровень риска: {entry.risk_level}")
        lines.append(f"  Причина: {entry.reason}")
        lines.append(f"  Flow: {_short_id(entry.flow_id)}")
        lines.append("───────────────────────────")
    for line in lines:
        renderer.render_message(ChatMessageRole.INFO, line)


async def pick_approval(
    gateway: Any,
    renderer: Any,
    approvals: list[Any],
    key_bridge: Any = None,
) -> tuple[PickerAction | None, ApprovalEntry | None]:
    """High-level entry point: show picker and return the user's decision.

    Returns ``(None, None)`` if the picker was cancelled or no approvals exist.
    """
    entries = build_picker_entries(approvals, gateway)
    if not entries:
        renderer.render_message(ChatMessageRole.INFO, "Нет ожидающих подтверждений.")
        return None, None
    if len(entries) == 1:
        return await show_single_approval_picker(gateway, renderer, entries[0], key_bridge=key_bridge)
    return await show_multi_approval_picker(gateway, renderer, entries, key_bridge=key_bridge)


__all__ = [
    "PickerAction",
    "ApprovalEntry",
    "PickerState",
    "build_picker_entries",
    "show_single_approval_picker",
    "show_multi_approval_picker",
    "pick_approval",
]
