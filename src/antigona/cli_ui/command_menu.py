"""Interactive slash-command menu with detailed command cards."""

from __future__ import annotations

import shutil
from collections.abc import Callable, Mapping, Sequence
from typing import Final

from prompt_toolkit.application.current import get_app
from prompt_toolkit.formatted_text import HTML

from antigona.cli_ui.prompts import DEFAULT_SLASH_COMMANDS, SlashCommand

_CATEGORY_ORDER: Final[dict[str, int]] = {
    "tasks": 0,
    "approvals": 1,
    "system": 2,
    "context": 3,
    "ui": 4,
}

LOCAL_UI_COMMANDS: Final[tuple[SlashCommand, ...]] = (
    SlashCommand(
        name="/clear",
        description="Очистить экран и консоль",
        usage="/clear",
        category="ui",
        emoji="🖌",
    ),
    SlashCommand(
        name="/theme",
        description="Сменить тему оформления: neon|minimal",
        usage="/theme <neon|minimal>",
        category="ui",
        emoji="🎨",
    ),
    SlashCommand(
        name="/portrait",
        description="Переключение профилей портрета Антигоны",
        usage="/portrait [full|large|medium|compact|mini|off]",
        category="ui",
        emoji="🖼",
    ),
    SlashCommand(
        name="/monitor",
        description="Панель микро-мониторинга ресурсов хоста",
        usage="/monitor",
        category="system",
        emoji="📊",
    ),
    SlashCommand(
        name="/verifier",
        description="Состояние службы верификации (Verifier Service)",
        usage="/verifier",
        category="system",
        emoji="🛡",
    ),
    SlashCommand(
        name="/cron",
        description="Планировщик фоновых задач cron",
        usage="/cron [list|add]",
        category="system",
        emoji="⏰",
    ),
    SlashCommand(
        name="/shell",
        description="Выполнение хост-команд владельца (Owner Mode)",
        usage="/shell <command>",
        category="system",
        emoji="💻",
    ),
    SlashCommand(
        name="/compact",
        description="Сжатие истории диалога и памяти",
        usage="/compact",
        category="context",
        emoji="📐",
    ),
    SlashCommand(
        name="/restart",
        description="Перезапуск сервисов стека Antigona",
        usage="/restart",
        category="system",
        emoji="🔄",
    ),
)


def _category_sort_key(cmd: SlashCommand) -> tuple[int, str]:
    return (_CATEGORY_ORDER.get(cmd.category, 99), cmd.name)


def _as_str_tuple(value: object) -> tuple[str, ...]:
    """Convert a JSON list field (or missing value) into a tuple of strings."""
    if isinstance(value, (list, tuple)):
        return tuple(str(v) for v in value)
    return ()


def build_slash_catalog(commands: Sequence[Mapping[str, object]]) -> tuple[SlashCommand, ...]:
    """Convert Gateway DTOs to SlashCommand tuples, deduplicated and sorted."""
    seen: set[str] = set()
    result: list[SlashCommand] = []
    for raw in commands:
        name = str(raw.get("name", "")).strip()
        if not name:
            continue
        if name in seen:
            continue
        seen.add(name)
        result.append(
            SlashCommand(
                name="/" + name,
                description=str(raw.get("description", "")),
                usage=str(raw.get("usage", "") or "/" + name),
                arguments=_as_str_tuple(raw.get("arguments")),
                flags=_as_str_tuple(raw.get("flags")),
                examples=_as_str_tuple(raw.get("examples")),
                category=str(raw.get("category", "")),
                emoji=str(raw.get("emoji", "")),
                long_description=str(raw.get("long_description", "")),
                read_only=bool(raw.get("read_only", False)),
                idempotent=bool(raw.get("idempotent", False)),
                destructive=bool(raw.get("destructive", False)),
                requires_approval=bool(raw.get("requires_approval", False)),
                risk_level=str(raw.get("risk_level", "LOW")),
            )
        )
    result.sort(key=_category_sort_key)
    return tuple(result)


def merge_catalog(
    gateway_commands: Sequence[Mapping[str, object]] | None,
) -> tuple[SlashCommand, ...]:
    """Merge gateway commands with local UI commands, deduplicated."""
    if gateway_commands is not None:
        base = list(build_slash_catalog(gateway_commands))
    else:
        base = list(DEFAULT_SLASH_COMMANDS)
    base.extend(LOCAL_UI_COMMANDS)
    seen: set[str] = set()
    merged: list[SlashCommand] = []
    for cmd in base:
        if cmd.name in seen:
            continue
        seen.add(cmd.name)
        merged.append(cmd)
    return tuple(merged)


def _truncate(text: str, width: int) -> str:
    """Truncate text to fit width, adding ellipsis if needed."""
    if len(text) <= width:
        return text
    return text[: width - 1] + "…"


def _build_card(cmd: SlashCommand, width: int) -> str:
    """Build a multi-line card for a command."""
    inner_width = max(width - 2, 1)
    lines: list[str] = []

    header = f"╭─ {cmd.name}"
    header += "─" * max(inner_width - len(header) + 1, 0)
    lines.append(header + "─╮")

    desc = f"{cmd.emoji} {cmd.description}" if cmd.emoji else cmd.description
    lines.append("│ " + _truncate(desc, inner_width).ljust(inner_width) + " │")

    if cmd.long_description:
        lines.append("│ " + _truncate(cmd.long_description, inner_width).ljust(inner_width) + " │")

    if cmd.usage:
        lines.append("│ " + _truncate(f"Использование: {cmd.usage}", inner_width).ljust(inner_width) + " │")

    if cmd.arguments:
        args = ", ".join(cmd.arguments)
        lines.append("│ " + _truncate(f"Аргументы: {args}", inner_width).ljust(inner_width) + " │")

    if cmd.flags:
        flags = ", ".join(cmd.flags)
        lines.append("│ " + _truncate(f"Флаги: {flags}", inner_width).ljust(inner_width) + " │")

    if cmd.examples:
        examples = " · ".join(cmd.examples)
        lines.append("│ " + _truncate(f"Примеры: {examples}", inner_width).ljust(inner_width) + " │")

    risk_parts: list[str] = [f"Риск: {cmd.risk_level}"]
    if cmd.read_only:
        risk_parts.append("RO")
    if cmd.destructive:
        risk_parts.append("Destr")
    if cmd.requires_approval:
        risk_parts.append("Approve")
    risk_line = " · ".join(risk_parts)
    lines.append("│ " + _truncate(risk_line, inner_width).ljust(inner_width) + " │")

    lines.append("╰" + "─" * inner_width + "╯")
    return "\n".join(lines)


def build_command_help(cmd: SlashCommand, width: int) -> str:
    """Return the compact help card for a single slash command.

    Public entry point shared by the PromptSession toolbar (completion-based)
    and the full-screen layout's ``/`` menu; both show name, description,
    parameters, usage and expected result for the selected command.
    """
    return _build_card(cmd, width)


def build_command_toolbar(
    catalog: Sequence[SlashCommand],
    fallback: Callable[[], HTML],
) -> Callable[[], HTML]:
    """Return a toolbar function that shows a command card when a completion is selected."""

    def toolbar() -> HTML:
        app = get_app()
        buf = app.current_buffer
        cs = buf.complete_state
        if cs is None:
            return fallback()
        completions = cs.completions
        index = cs.complete_index
        if index is None or index < 0 or index >= len(completions):
            return fallback()
        selected_text = completions[index].text
        cmd = next((c for c in catalog if c.name == selected_text), None)
        if cmd is None:
            return fallback()

        width = min(shutil.get_terminal_size().columns, 100)
        card = _build_card(cmd, width)
        return HTML(card.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

    return toolbar


__all__ = [
    "LOCAL_UI_COMMANDS",
    "build_command_help",
    "build_command_toolbar",
    "build_slash_catalog",
    "merge_catalog",
]
