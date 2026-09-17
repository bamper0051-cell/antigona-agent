"""Prompt line reader and input normalization for pure CLI UI.

Provides normalized prompt reading with prompt_toolkit integration,
deterministic slash-command completion, history strictly disabled,
EOF / Ctrl+C signal handling, and zero raw input logging or persistence.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Final

from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.filters import completion_is_selected
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.history import DummyHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.key_processor import KeyPressEvent
from prompt_toolkit.shortcuts import PromptSession

#: Prompt history is strictly disabled (no history file loading or saving).
PROMPT_HISTORY_ENABLED: Final[bool] = False


@dataclass(frozen=True)
class SlashCommand:
    """Immutable descriptor for a supported slash command."""

    name: str
    description: str = ""
    usage: str = ""
    arguments: tuple[str, ...] = ()
    flags: tuple[str, ...] = ()
    examples: tuple[str, ...] = ()
    category: str = ""
    emoji: str = ""
    long_description: str = ""
    read_only: bool = False
    idempotent: bool = False
    destructive: bool = False
    requires_approval: bool = False
    risk_level: str = "LOW"


DEFAULT_SLASH_COMMANDS: Final[tuple[SlashCommand, ...]] = (
    SlashCommand("/help", "❓ Все команды"),
    SlashCommand("/exit", "🚪 Выход"),
    SlashCommand("/quit", "🚪 Выход"),
    SlashCommand("/status", "📊 Состояние задачи", " /status <flow_id>"),
    SlashCommand("/list", "📌 Активные задачи"),
    SlashCommand("/tasks", "📌 Активные задачи (алиас)"),
    SlashCommand("/get", "📄 Задача по ID", " /get <flow_id>"),
    SlashCommand("/cancel", "⛔ Отменить задачу", " /cancel <flow_id>"),
    SlashCommand("/steer", "🧭 Изменить направление задачи", " /steer <flow_id> <текст>"),
    SlashCommand("/approvals", "🔐 Ожидающие подтверждения"),
    SlashCommand("/approve", "✅ Одобрить действие", " /approve <flow_id>"),
    SlashCommand("/deny", "❌ Отклонить действие", " /deny <flow_id>"),
    SlashCommand("/health", "❤️ Проверка Gateway"),
    SlashCommand("/commands", "🧰 Реестр команд"),
    SlashCommand("/session", "💬 Текущая сессия", " /session <session_id>"),
    SlashCommand("/history", "🕘 История сессии", " /history <session_id>"),
    SlashCommand("/memory", "🧠 Память агента", " /memory [текст]"),
    SlashCommand("/model", "🤖 Текущая модель или её переключение", " /model [name]"),
    SlashCommand("/install", "🔧 Установить инструмент в песочницу", " /install <pip|npm|apt> <пакет>"),
    SlashCommand("/image", "🎨 Сгенерировать картинку", " /image <описание>"),
    SlashCommand("/tts", "🎙 Отправить голосовое сообщение (TTS)", " /tts <текст>"),
    SlashCommand("/web", "🌐 Поиск в интернете", " /web <запрос>"),
    SlashCommand("/ollama", "🤖 Управление локальным LLM (Ollama)", " /ollama <status|start|list|pull|switch> [модель]"),
    SlashCommand("/mcp", "🗂 Управление MCP-серверами", " /mcp list|add|remove"),
    SlashCommand("/plugins", "🧩 Управление плагинами", " /plugins list|load|unload"),
    SlashCommand("/cli", "🖥 Справка по CLI-командам", " /cli"),
    SlashCommand("/provider", "🏭 Список и выбор LLM-провайдера", " /provider [name]"),
    SlashCommand("/providers", "🏭 Список всех LLM-провайдеров", " /providers"),
    SlashCommand("/setllm", "⚡ Переключить провайдера / модель", " /setllm <provider> [model]"),
    SlashCommand("/bot", "🤖 Информация о боте"),
    SlashCommand("/portrait", "🖼 Профиль портрета Антигоны", " /portrait [full|large|medium|compact|mini|off]"),
    SlashCommand("/monitor", "📊 Мини-мониторинг ресурсов", " /monitor"),
    SlashCommand("/verifier", "🛡 Служба верификации", " /verifier"),
    SlashCommand("/skills", "🧰 Набор скиллов", " /skills [list]"),
    SlashCommand("/cron", "⏰ Расписание Cron", " /cron [list]"),
    SlashCommand("/shell", "💻 Команды хоста владельца", " /shell <command>"),
    SlashCommand("/compact", "📐 Сжать память диалога", " /compact"),
    SlashCommand("/restart", "🔄 Перезапустить сервисы", " /restart"),
    SlashCommand("/clear", "🖌 Очистить экран", " /clear", category="ui"),
    SlashCommand("/uptime", "🖥 Система (аптайм/load/RAM/диск)", " /uptime", category="ui", read_only=True),
    SlashCommand("/sys", "🖥 Алиас /uptime", " /sys", category="ui", read_only=True),
    SlashCommand("/export", "💾 Экспорт диалога в markdown", " /export [path]", category="ui", read_only=True),
    SlashCommand("/theme", "🎨 Тема терминала", " /theme [name]", category="ui", read_only=True),
    SlashCommand("/alias", "🔗 Алиасы команд", " /alias [name cmd]", category="ui", read_only=True),
    SlashCommand("/sessions", "💬 Недавние сессии", " /sessions", category="ui", read_only=True),
    SlashCommand(
        "/hermes",
        "🤖 Hermes RCA: диагностика ошибок",
        " /hermes [last|trace <correlation_id>|explain <error_id>|evidence <rca_id>|suggest-fix <rca_id>]",
        category="diagnostics",
        read_only=True,
        idempotent=True,
        examples=("/hermes last", "/hermes trace abc", "/hermes explain err_abc", "/hermes evidence rca_1", "/hermes suggest-fix rca_1"),
    ),
)


class SlashCommandCompleter(Completer):
    """Deterministic slash-command completer."""

    def __init__(
        self,
        commands: Sequence[SlashCommand] | Iterable[SlashCommand] = DEFAULT_SLASH_COMMANDS,
    ) -> None:
        self._commands: tuple[SlashCommand, ...] = tuple(commands)

    @property
    def commands(self) -> tuple[SlashCommand, ...]:
        return self._commands

    def get_completions(
        self, document: Document, complete_event: Any = None
    ) -> Iterable[Completion]:
        text_before_cursor = document.text_before_cursor

        # Arg completion: offer theme names after "/theme " (and "/theme custom "
        # is a fixed hint). Lazily import to avoid import-order cycles.
        if text_before_cursor.startswith("/theme "):
            arg = text_before_cursor[len("/theme ") :]
            if arg.startswith("custom "):
                return
            from antigona.cli_ui import themes

            for name in themes.theme_names():
                if name.startswith(arg):
                    yield Completion(name, start_position=-len(arg))
            return

        if not text_before_cursor.startswith("/"):
            return

        if " " in text_before_cursor:
            return

        search_term = text_before_cursor
        for cmd in self._commands:
            if cmd.name.startswith(search_term):
                display = cmd.name + (" " + cmd.emoji if cmd.emoji else "")
                meta_parts: list[str] = []
                if cmd.emoji:
                    meta_parts.append(cmd.emoji)
                if cmd.description:
                    meta_parts.append(cmd.description)
                if cmd.usage:
                    meta_parts.append("— " + cmd.usage)
                yield Completion(
                    cmd.name,
                    start_position=-len(search_term),
                    display=display,
                    display_meta=" ".join(meta_parts),
                )


def create_prompt_key_bindings() -> KeyBindings:
    """Create key bindings for PromptSession supporting multiline input.

    Single-line UX:
    - Pressing Enter validates and submits the input (standard single-line UX).

    Multiline UX:
    - Pressing Escape then Enter (or Alt+Enter / Meta+Enter) inserts a literal newline (\\n).
    - Pasting text with newlines (e.g. bracketed paste) preserves all newlines.
    - When ready, pressing Enter submits the full multiline text as one turn.
    """
    kb = KeyBindings()

    @kb.add("escape", "enter")
    def _handle_esc_enter(event: KeyPressEvent) -> None:
        event.current_buffer.insert_text("\n")

    @kb.add("enter", filter=~completion_is_selected)
    def _handle_enter(event: KeyPressEvent) -> None:
        event.current_buffer.validate_and_handle()

    return kb


def default_prompt_continuation(width: int, line_number: int, is_soft_wrap: bool) -> HTML:
    """Continuation prompt line indicator for multiline input."""
    return HTML('<style fg="#7C3AED"> ⚡ antigona … </style>')


def create_prompt_session(
    catalog: Sequence[SlashCommand] | Iterable[SlashCommand] = DEFAULT_SLASH_COMMANDS,
    bottom_toolbar: Any | None = None,
    *,
    multiline: bool = True,
    input: Any | None = None,
    output: Any | None = None,
) -> PromptSession[str]:
    """Create a prompt_toolkit PromptSession with history strictly disabled.

    *bottom_toolbar* — optional callable returning a bottom-toolbar renderable
    (e.g. ``build_status_bar``).  It is re-evaluated by prompt_toolkit on each
    UI refresh (~100ms) while the prompt is displayed, giving a persistent,
    animated status line across the whole chat session.

    *multiline* — enables multiline input (Esc+Enter / Alt+Enter inserts newline,
    Enter submits single or multiline buffer).

    *input* / *output* — optional prompt_toolkit input/output objects for headless
    or pipe testing.
    """
    completer = SlashCommandCompleter(catalog)
    key_bindings = create_prompt_key_bindings() if multiline else None
    session_kwargs: dict[str, Any] = {
        "history": DummyHistory(),
        "completer": completer,
        "reserve_space_for_menu": 0,
        "bottom_toolbar": bottom_toolbar,
        "complete_while_typing": True,
        "multiline": multiline,
        "key_bindings": key_bindings,
        "prompt_continuation": default_prompt_continuation if multiline else None,
    }
    if input is not None:
        session_kwargs["input"] = input
    if output is not None:
        session_kwargs["output"] = output
    return PromptSession(**session_kwargs)


def normalize_prompt_input(raw: str | None) -> str:
    """Normalize raw prompt input string.

    Returns control character "\\x0c" strictly when raw input is exactly "\\x0c".
    Otherwise returns raw input untouched.
    """
    if raw is None:
        return ""

    if raw == "\x0c":
        return "\x0c"

    return raw


async def read_prompt(
    prompt_str: str | HTML = "> ",
    session: PromptSession[str] | None = None,
    *,
    is_password: bool = False,
) -> str:
    """Read a single line from prompt with history disabled and safe signal handling.

    *prompt_str* accepts prompt-toolkit formatted text (e.g. an ``HTML``
    object) so the input line can be highlighted like a status bar.

    *is_password* — when True, the typed input is masked (displayed as
    ``****``) via prompt_toolkit, e.g. for hidden PIN entry. Only takes
    effect when a PromptSession is supplied; the plain ``input`` fallback
    cannot mask input.

    Returns normalized input string, or special control characters:
    - ``"\\x03"`` for Ctrl+C (KeyboardInterrupt)
    - ``"\\x04"`` for EOF (EOFError)
    """
    try:
        if session is not None:
            raw = await session.prompt_async(prompt_str, is_password=is_password)
        else:
            raw = await asyncio.to_thread(input, str(prompt_str))
        return normalize_prompt_input(raw)
    except KeyboardInterrupt:
        return "\x03"
    except EOFError:
        return "\x04"


__all__ = [
    "DEFAULT_SLASH_COMMANDS",
    "PROMPT_HISTORY_ENABLED",
    "SlashCommand",
    "SlashCommandCompleter",
    "create_prompt_key_bindings",
    "create_prompt_session",
    "default_prompt_continuation",
    "normalize_prompt_input",
    "read_prompt",
]
