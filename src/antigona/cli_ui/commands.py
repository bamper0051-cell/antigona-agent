"""Bounded parser and safe thin command dispatcher for CLI UI.

Implements input classification, strict fail-closed parsing, and safe async dispatch
through an injected canonical GatewayClient Protocol without raw input leaks or
terminal success calculation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

_RESOURCE_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{1,36}$")

#: Alias identifier: letters/digits/underscore/hyphen, no spaces, no leading "/".
_VALID_ALIAS_NAME = re.compile(r"^[A-Za-z0-9_-]{1,24}$").match


def is_valid_resource_id(resource_id: str) -> bool:
    """Validate a resource ID parameter as a single safe URL path segment.

    Must be ASCII alphanumeric plus hyphen and underscore only, length 1..36.
    Rejects slash, backslash, dots/traversal, ?, #, %, control chars,
    whitespace/padding, encoded traversal, empty strings, and overlength strings.
    """
    if not isinstance(resource_id, str):
        return False
    if not resource_id.isascii():
        return False
    return bool(_RESOURCE_ID_PATTERN.fullmatch(resource_id))


class CommandKind(StrEnum):
    """Classification of CLI commands."""

    NOOP = "NOOP"
    EXIT = "EXIT"
    CTRL_C = "CTRL_C"
    REDRAW = "REDRAW"
    HELP = "HELP"
    STATUS = "STATUS"
    LIST = "LIST"
    GET = "GET"
    CANCEL = "CANCEL"
    APPROVALS = "APPROVALS"
    APPROVE = "APPROVE"
    DENY = "DENY"
    STEER = "STEER"
    TASKS = "TASKS"
    HEALTH = "HEALTH"
    COMMANDS = "COMMANDS"
    SESSION = "SESSION"
    HISTORY = "HISTORY"
    MEMORY = "MEMORY"
    INFORMATIONAL = "INFORMATIONAL"
    SHELL = "SHELL"
    UPTIME = "UPTIME"
    EXPORT = "EXPORT"
    THEME = "THEME"
    ALIAS = "ALIAS"
    SESSIONS = "SESSIONS"
    SETLLM = "SETLLM"
    PROVIDER = "PROVIDER"
    MODEL = "MODEL"
    INSTALL = "INSTALL"
    MCP = "MCP"
    PLUGINS = "PLUGINS"
    SKILLS = "SKILLS"
    CLI = "CLI"
    UNSUPPORTED = "UNSUPPORTED"
    MALFORMED = "MALFORMED"
    UNKNOWN = "UNKNOWN"


class CommandDisposition(StrEnum):
    """Disposition outcome of command dispatch.

    Must NOT contain or manufacture terminal success.
    """

    LOCAL_ACTION = "LOCAL_ACTION"
    GATEWAY_EXECUTION = "GATEWAY_EXECUTION"
    FAIL_CLOSED = "FAIL_CLOSED"
    ERROR = "ERROR"


@dataclass(frozen=True)
class ParsedCommand:
    """Typed result of command parsing."""

    kind: CommandKind
    args: tuple[str, ...] = ()
    command_name: str = ""


@dataclass(frozen=True)
class DispatchResult:
    """Result of safe thin command dispatch."""

    disposition: CommandDisposition
    kind: CommandKind
    data: Any = None
    error: str | None = None


@runtime_checkable
class GatewayClientProtocol(Protocol):
    """Structural protocol for injected GatewayClient interaction.

    All canonical GatewayClient methods are async.
    """

    async def get_flow(self, flow_id: str) -> Any: ...

    async def list_flows(
        self,
        conversation_id: str | Any | None = "",
        status: Any | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Any: ...

    async def cancel(self, flow_id: str, reason: str = "") -> Any: ...

    async def list_approvals(
        self,
        status: str = "PENDING",
        limit: int = 50,
        offset: int = 0,
    ) -> Any: ...

    async def get_approval(self, approval_id: str) -> Any: ...

    async def decide_approval(self, approval_id: str, approve: bool) -> Any: ...

    async def submit_task(
        self,
        message: str,
        conversation_id: str = "",
        client: str = "cli",
        metadata: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> Any: ...

    async def send_dialogue_turn(
        self,
        text: str,
        session_id: str,
        channel: str = "cli",
        user_id: str = "default",
        turn_id: str = "",
    ) -> Any: ...

    async def steer(self, flow_id: str, command: Any) -> Any: ...

    async def health(self) -> Any: ...

    async def get_events(self, after_seq: int = 0, limit: int = 200) -> Any: ...

    async def list_commands(self, channel: str = "all") -> Any: ...

    async def session_info(self, session_id: str) -> Any: ...

    async def session_history(self, session_id: str, limit: int = 100) -> Any: ...

    async def memory_list(self, *, kind: str | None = None, query: str | None = None, limit: int = 50) -> Any: ...


def parse_command(raw_input: str) -> ParsedCommand:
    """Normalize, classify and parse raw input into a typed ParsedCommand.

    Raw input is NOT stripped of leading/trailing whitespace before classification:
    the raw text is passed through untouched.  Blank/whitespace-only input, control
    characters, and the REDRAW marker (exact ``\\x0c``) are all handled before any
    text classification, so whitespace padding around a slash command stays
    distinguishable from the clean form and fails closed as UNSUPPORTED.

    Fails closed on unknown commands, malformed arguments, generic text, and
    unsupported inputs.
    """
    if not raw_input:
        return ParsedCommand(kind=CommandKind.NOOP)

    if raw_input == "\x0c":
        return ParsedCommand(kind=CommandKind.REDRAW, command_name="redraw")

    if raw_input == "\x04":
        return ParsedCommand(kind=CommandKind.EXIT, command_name="exit")

    if raw_input == "\x03":
        return ParsedCommand(kind=CommandKind.CTRL_C, command_name="ctrl_c")

    # Blank/whitespace-only check WITHOUT stripping the raw input.
    # This check is purely for emptiness detection; the raw input is never modified.
    if not raw_input.strip():
        return ParsedCommand(kind=CommandKind.NOOP)

    cleaned = raw_input.strip()

    # Structural malformed check: embedded newlines or NUL bytes fail closed
    if cleaned.startswith("/") and ("\n" in raw_input or "\r" in raw_input or "\0" in raw_input):
        verb = cleaned.split()[0].lower() if cleaned.split() else "/"
        return ParsedCommand(kind=CommandKind.MALFORMED, args=(), command_name=verb)

    # Outer whitespace is normalized — non-slash input classifies as UNSUPPORTED text
    if not cleaned.startswith("/"):
        return ParsedCommand(kind=CommandKind.UNSUPPORTED, command_name="text")

    tokens = cleaned.split()
    verb = tokens[0].lower()
    raw_args = tuple(tokens[1:])

    def parse_id_bearing(kind: CommandKind, cmd_name: str) -> ParsedCommand:
        parts = cleaned.split(" ")
        if len(parts) == 2 and parts[0].lower() == cmd_name and is_valid_resource_id(parts[1]):
            return ParsedCommand(kind=kind, args=(parts[1],), command_name=cmd_name)
        return ParsedCommand(kind=CommandKind.MALFORMED, args=raw_args, command_name=cmd_name)

    match verb:
        case "/help":
            if not raw_args:
                return ParsedCommand(kind=CommandKind.HELP, command_name="/help")
            return ParsedCommand(kind=CommandKind.MALFORMED, args=raw_args, command_name="/help")
        case "/exit" | "/quit":
            if not raw_args:
                return ParsedCommand(kind=CommandKind.EXIT, command_name=verb)
            return ParsedCommand(kind=CommandKind.MALFORMED, args=raw_args, command_name=verb)
        case "/status":
            if not raw_args:
                return ParsedCommand(kind=CommandKind.MALFORMED, args=(), command_name="/status")
            return parse_id_bearing(CommandKind.STATUS, "/status")
        case "/list":
            if not raw_args:
                return ParsedCommand(kind=CommandKind.LIST, command_name="/list")
            return ParsedCommand(kind=CommandKind.MALFORMED, args=raw_args, command_name="/list")
        case "/get":
            return parse_id_bearing(CommandKind.GET, "/get")
        case "/cancel":
            return parse_id_bearing(CommandKind.CANCEL, "/cancel")
        case "/approvals":
            if not raw_args:
                return ParsedCommand(kind=CommandKind.APPROVALS, command_name="/approvals")
            return ParsedCommand(
                kind=CommandKind.MALFORMED, args=raw_args, command_name="/approvals"
            )
        case "/approve":
            if not raw_args:
                return ParsedCommand(kind=CommandKind.APPROVE, command_name="/approve")
            return parse_id_bearing(CommandKind.APPROVE, "/approve")
        case "/deny":
            if not raw_args:
                return ParsedCommand(kind=CommandKind.DENY, command_name="/deny")
            return parse_id_bearing(CommandKind.DENY, "/deny")
        case "/steer":
            if len(raw_args) >= 2 and is_valid_resource_id(raw_args[0]):
                return ParsedCommand(
                    kind=CommandKind.STEER,
                    args=(raw_args[0], " ".join(raw_args[1:])),
                    command_name="/steer",
                )
            return ParsedCommand(kind=CommandKind.MALFORMED, args=raw_args, command_name="/steer")
        case "/tasks":
            if not raw_args:
                return ParsedCommand(kind=CommandKind.TASKS, command_name="/tasks")
            return ParsedCommand(kind=CommandKind.MALFORMED, args=raw_args, command_name="/tasks")
        case "/health":
            if not raw_args:
                return ParsedCommand(kind=CommandKind.HEALTH, command_name="/health")
            return ParsedCommand(kind=CommandKind.MALFORMED, args=raw_args, command_name="/health")
        case "/commands":
            if not raw_args:
                return ParsedCommand(kind=CommandKind.COMMANDS, command_name="/commands")
            return ParsedCommand(
                kind=CommandKind.MALFORMED, args=raw_args, command_name="/commands"
            )
        case "/session":
            return parse_id_bearing(CommandKind.SESSION, "/session")
        case "/history":
            return parse_id_bearing(CommandKind.HISTORY, "/history")
        case "/memory":
            return ParsedCommand(kind=CommandKind.MEMORY, args=raw_args, command_name="/memory")
        # Informational command intents — routed to the conversation handler by
        # the server brain (command.model_select / command.providers / command.bot).
        case "/setllm":
            return ParsedCommand(kind=CommandKind.SETLLM, args=tuple(raw_args), command_name="/setllm")
        case "/provider" | "/providers":
            return ParsedCommand(kind=CommandKind.PROVIDER, args=tuple(raw_args), command_name=verb)
        case "/model":
            return ParsedCommand(kind=CommandKind.MODEL, args=tuple(raw_args), command_name="/model")
        case "/install":
            return ParsedCommand(kind=CommandKind.INSTALL, args=tuple(raw_args), command_name="/install")
        case "/mcp":
            return ParsedCommand(kind=CommandKind.MCP, args=tuple(raw_args), command_name="/mcp")
        case "/plugins":
            return ParsedCommand(kind=CommandKind.PLUGINS, args=tuple(raw_args), command_name="/plugins")
        case "/skills":
            return ParsedCommand(kind=CommandKind.SKILLS, args=tuple(raw_args), command_name="/skills")
        case "/cli":
            return ParsedCommand(kind=CommandKind.CLI, args=tuple(raw_args), command_name="/cli")
        case "/bot" | "/keys":
            if raw_args:
                return ParsedCommand(kind=CommandKind.MALFORMED, args=raw_args, command_name=verb)
            return ParsedCommand(kind=CommandKind.INFORMATIONAL, command_name=verb)
        case "/shell" | "/sh":
            if not raw_args:
                return ParsedCommand(kind=CommandKind.MALFORMED, args=(), command_name="/shell")
            return ParsedCommand(
                kind=CommandKind.SHELL,
                args=(" ".join(raw_args),),
                command_name="/shell",
            )
        case "/clear":
            if not raw_args:
                return ParsedCommand(kind=CommandKind.REDRAW, command_name="/clear")
            return ParsedCommand(kind=CommandKind.MALFORMED, args=raw_args, command_name="/clear")
        case "/uptime" | "/sys":
            if not raw_args:
                return ParsedCommand(kind=CommandKind.UPTIME, command_name="/uptime")
            return ParsedCommand(kind=CommandKind.MALFORMED, args=raw_args, command_name="/uptime")
        case "/export":
            if len(raw_args) <= 1:
                return ParsedCommand(kind=CommandKind.EXPORT, args=raw_args, command_name="/export")
            return ParsedCommand(kind=CommandKind.MALFORMED, args=raw_args, command_name="/export")
        case "/theme":
            if len(raw_args) <= 1:
                return ParsedCommand(kind=CommandKind.THEME, args=raw_args, command_name="/theme")
            return ParsedCommand(kind=CommandKind.MALFORMED, args=raw_args, command_name="/theme")
        case "/sessions":
            if not raw_args:
                return ParsedCommand(kind=CommandKind.SESSIONS, args=(), command_name="/sessions")
            return ParsedCommand(kind=CommandKind.MALFORMED, args=raw_args, command_name="/sessions")
        case "/alias":
            if not raw_args:
                return ParsedCommand(kind=CommandKind.ALIAS, args=(), command_name="/alias")
            if raw_args[0] == "--del" and len(raw_args) == 2 and is_valid_resource_id(raw_args[1]):
                return ParsedCommand(kind=CommandKind.ALIAS, args=("--del", raw_args[1]), command_name="/alias")
            if len(raw_args) >= 2 and _VALID_ALIAS_NAME(raw_args[0]):
                return ParsedCommand(kind=CommandKind.ALIAS, args=(raw_args[0], " ".join(raw_args[1:])), command_name="/alias")
            return ParsedCommand(kind=CommandKind.MALFORMED, args=raw_args, command_name="/alias")
        case _:
            return ParsedCommand(kind=CommandKind.UNKNOWN, args=raw_args, command_name=verb)


#: Full help text: every supported command with a hint and usage line.
HELP_TEXT: str = (
    "Список команд Antigona CLI:\n"
    "\n"
    "  /help                 — эта справка\n"
    "  /exit, /quit          — выйти из интерфейса\n"
    "  /status <flow_id>     — статус конкретной задачи\n"
    "  /list                 — список задач\n"
    "  /tasks                — список задач (алиас /list)\n"
    "  /get <flow_id>        — детали задачи по ID\n"
    "  /cancel <flow_id>     — отменить задачу\n"
    "  /steer <flow_id> <текст> — скорректировать выполняющуюся задачу\n"
    "  /approvals            — список ожидающих одобрений\n"
    "  /approve <approval_id> — одобрить операцию\n"
    "  /deny <approval_id>   — отклонить операцию\n"
    "  /health               — проверка доступности Gateway\n"
    "  /commands             — список команд системы (из реестра)\n"
    "  /session <session_id> — информация о сессии\n"
    "  /history <session_id> — история переписки сессии\n"
    "  /memory [текст]       — память агента (посмотреть / записать)\n"
    "  /model                — текущая модель (диалог)\n"
    "  /install <mgr> <пакет> — установить инструмент в песочницу (pip|npm|apt)\n"
    "  /mcp [list|add|remove] — управление MCP-серверами\n"
    "  /plugins [list|load|unload] — управление плагинами\n"
    "  /cli                   — справка по CLI-командам\n"
    "  /providers            — доступные LLM-провайдеры (диалог)\n"
    "  /bot                  — информация о боте (диалог)\n"
    "  /shell <команда>      — хост-shell владельца (требует PIN owner mode)\n"
    "  /uptime, /sys         — системная инфа (аптайм/load/RAM/диск, read-only)\n"
    "  /export [path]        — экспорт диалога в markdown-файл (в ~)\n"
    "  /theme [name]         — тема терминала (без имени — список; e.g. neon/hacker)\n"
    "  /theme custom <hex..> — свой дизайн; /theme save <имя> — сохранить как тему\n"
    "  /alias [name cmd]     — алиасы команд; /alias --del <name> удалить\n"
    "  /sessions             — список недавних сессий\n"
    "\n"
    "Свободный текст отправляется как диалоговый turn: задачи, вопросы и\n"
    "команды на естественном языке обрабатывает ядро Antigona.  Пока задача\n"
    "ждёт одобрения, ответь «да» или «нет» без ID.\n"
)


async def dispatch_command(
    cmd: ParsedCommand,
    gateway_client: GatewayClientProtocol | None = None,
) -> DispatchResult:
    """Safely dispatch a ParsedCommand asynchronously.

    Local actions return local results.
    Gateway commands dispatch through the injected async GatewayClient.
    Unsupported/malformed inputs and exceptions fail closed safely.
    """
    match cmd.kind:
        case CommandKind.NOOP:
            return DispatchResult(
                disposition=CommandDisposition.LOCAL_ACTION, kind=CommandKind.NOOP, data="NOOP"
            )
        case CommandKind.EXIT:
            return DispatchResult(
                disposition=CommandDisposition.LOCAL_ACTION, kind=CommandKind.EXIT, data="EXIT"
            )
        case CommandKind.CTRL_C:
            return DispatchResult(
                disposition=CommandDisposition.LOCAL_ACTION, kind=CommandKind.CTRL_C, data="CTRL_C"
            )
        case CommandKind.REDRAW:
            return DispatchResult(
                disposition=CommandDisposition.LOCAL_ACTION, kind=CommandKind.REDRAW, data="REDRAW"
            )
        case CommandKind.HELP:
            return DispatchResult(
                disposition=CommandDisposition.LOCAL_ACTION,
                kind=CommandKind.HELP,
                data=HELP_TEXT,
            )
        case CommandKind.UNSUPPORTED | CommandKind.MALFORMED:
            return DispatchResult(
                disposition=CommandDisposition.FAIL_CLOSED,
                kind=cmd.kind,
                error=f"Command fail-closed: unsupported or malformed command '{cmd.command_name}'",
            )
        case CommandKind.UNKNOWN:
            return DispatchResult(
                disposition=CommandDisposition.LOCAL_ACTION,
                kind=CommandKind.UNKNOWN,
                data=f"Unknown command: {cmd.command_name}. Did you mean: /help, /get, /list, /status, /approve, /cancel?",
            )

    if gateway_client is None:
        return DispatchResult(
            disposition=CommandDisposition.FAIL_CLOSED,
            kind=cmd.kind,
            error="Gateway client required for Gateway commands",
        )

    if not isinstance(cmd.args, tuple):
        return DispatchResult(
            disposition=CommandDisposition.FAIL_CLOSED,
            kind=cmd.kind,
            error="Command fail-closed: unsupported or malformed command",
        )

    try:
        match cmd.kind:
            case CommandKind.STEER:
                if (
                    len(cmd.args) == 2
                    and is_valid_resource_id(cmd.args[0])
                    and cmd.args[1].strip()
                ):
                    from antigona.core.control_plane import SteeringCommand

                    steering = SteeringCommand(
                        flow_id=cmd.args[0],
                        command="modify",
                        modification_text=cmd.args[1],
                        correlation_id="cli-steer",
                    )
                    res = await gateway_client.steer(cmd.args[0], steering)
                else:
                    return DispatchResult(
                        disposition=CommandDisposition.FAIL_CLOSED,
                        kind=cmd.kind,
                        error="Command fail-closed: unsupported or malformed command",
                    )
            case CommandKind.STATUS:
                if not cmd.args:
                    res = await gateway_client.list_flows()
                elif len(cmd.args) == 1 and is_valid_resource_id(cmd.args[0]):
                    res = await gateway_client.get_flow(cmd.args[0])
                else:
                    return DispatchResult(
                        disposition=CommandDisposition.FAIL_CLOSED,
                        kind=cmd.kind,
                        error="Command fail-closed: unsupported or malformed command",
                    )
            case CommandKind.LIST:
                if not cmd.args:
                    res = await gateway_client.list_flows()
                else:
                    return DispatchResult(
                        disposition=CommandDisposition.FAIL_CLOSED,
                        kind=cmd.kind,
                        error="Command fail-closed: unsupported or malformed command",
                    )
            case CommandKind.GET:
                if len(cmd.args) == 1 and is_valid_resource_id(cmd.args[0]):
                    res = await gateway_client.get_flow(cmd.args[0])
                else:
                    return DispatchResult(
                        disposition=CommandDisposition.FAIL_CLOSED,
                        kind=cmd.kind,
                        error="Command fail-closed: unsupported or malformed command",
                    )
            case CommandKind.CANCEL:
                if len(cmd.args) == 1 and is_valid_resource_id(cmd.args[0]):
                    res = await gateway_client.cancel(cmd.args[0])
                else:
                    return DispatchResult(
                        disposition=CommandDisposition.FAIL_CLOSED,
                        kind=cmd.kind,
                        error="Command fail-closed: unsupported or malformed command",
                    )
            case CommandKind.APPROVALS:
                if not cmd.args:
                    res = await gateway_client.list_approvals()
                else:
                    return DispatchResult(
                        disposition=CommandDisposition.FAIL_CLOSED,
                        kind=cmd.kind,
                        error="Command fail-closed: unsupported or malformed command",
                    )
            case CommandKind.APPROVE | CommandKind.DENY:
                approve = cmd.kind is CommandKind.APPROVE
                if len(cmd.args) == 1 and is_valid_resource_id(cmd.args[0]):
                    res = await gateway_client.decide_approval(cmd.args[0], approve=approve)
                elif not cmd.args:
                    approvals = await gateway_client.list_approvals()
                    # Real Gateway returns an ApprovalListView (pydantic)
                    # with ``items``; tolerate plain sequences too.
                    items = getattr(approvals, "items", None)
                    if items is None and isinstance(approvals, (list, tuple)):
                        items = approvals
                    items = list(items or [])
                    if not items:
                        return DispatchResult(
                            disposition=CommandDisposition.GATEWAY_EXECUTION,
                            kind=cmd.kind,
                            data={"message": "Нет ожидающих подтверждений."},
                        )
                    if len(items) == 1:
                        approval = items[0]
                        aid = (
                            approval["id"]
                            if isinstance(approval, dict)
                            else getattr(approval, "id", "")
                        )
                        if not aid:
                            return DispatchResult(
                                disposition=CommandDisposition.FAIL_CLOSED,
                                kind=cmd.kind,
                                error="Approval missing id",
                            )
                        res = await gateway_client.decide_approval(aid, approve=approve)
                    else:
                        return DispatchResult(
                            disposition=CommandDisposition.GATEWAY_EXECUTION,
                            kind=cmd.kind,
                            data={
                                "message": (
                                    f"Ожидают подтверждения: {len(items)}. "
                                    "Используйте /approvals для списка."
                                )
                            },
                        )
                else:
                    return DispatchResult(
                        disposition=CommandDisposition.FAIL_CLOSED,
                        kind=cmd.kind,
                        error="Command fail-closed: unsupported or malformed command",
                    )
            case CommandKind.TASKS:
                if not cmd.args:
                    res = await gateway_client.list_flows()
                else:
                    return DispatchResult(
                        disposition=CommandDisposition.FAIL_CLOSED,
                        kind=cmd.kind,
                        error="Command fail-closed: unsupported or malformed command",
                    )
            case CommandKind.HEALTH:
                if not cmd.args:
                    res = await gateway_client.health()
                else:
                    return DispatchResult(
                        disposition=CommandDisposition.FAIL_CLOSED,
                        kind=cmd.kind,
                        error="Command fail-closed: unsupported or malformed command",
                    )
            case CommandKind.COMMANDS:
                if not cmd.args:
                    res = await gateway_client.list_commands()
                else:
                    return DispatchResult(
                        disposition=CommandDisposition.FAIL_CLOSED,
                        kind=cmd.kind,
                        error="Command fail-closed: unsupported or malformed command",
                    )
            case CommandKind.SESSION:
                if len(cmd.args) == 1 and is_valid_resource_id(cmd.args[0]):
                    res = await gateway_client.session_info(cmd.args[0])
                else:
                    return DispatchResult(
                        disposition=CommandDisposition.FAIL_CLOSED,
                        kind=cmd.kind,
                        error="Command fail-closed: unsupported or malformed command",
                    )
            case CommandKind.HISTORY:
                if len(cmd.args) == 1 and is_valid_resource_id(cmd.args[0]):
                    res = await gateway_client.session_history(cmd.args[0])
                else:
                    return DispatchResult(
                        disposition=CommandDisposition.FAIL_CLOSED,
                        kind=cmd.kind,
                        error="Command fail-closed: unsupported or malformed command",
                    )
            case CommandKind.MEMORY:
                if len(cmd.args) <= 1:
                    res = await gateway_client.memory_list()
                else:
                    return DispatchResult(
                        disposition=CommandDisposition.FAIL_CLOSED,
                        kind=cmd.kind,
                        error="Command fail-closed: unsupported or malformed command",
                    )
            case _:
                return DispatchResult(
                    disposition=CommandDisposition.FAIL_CLOSED,
                    kind=cmd.kind,
                    error="Command fail-closed: unsupported operation",
                )

        return DispatchResult(
            disposition=CommandDisposition.GATEWAY_EXECUTION,
            kind=cmd.kind,
            data=res,
        )
    except Exception:
        return DispatchResult(
            disposition=CommandDisposition.ERROR,
            kind=cmd.kind,
            error="Gateway operation failed; details withheld: [REDACTED]",
        )


__all__ = [
    "CommandDisposition",
    "CommandKind",
    "DispatchResult",
    "GatewayClientProtocol",
    "HELP_TEXT",
    "ParsedCommand",
    "dispatch_command",
    "is_valid_resource_id",
    "parse_command",
]
