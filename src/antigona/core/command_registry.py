"""Единый реестр команд Antigona (CLI + Telegram)."""

from __future__ import annotations

from dataclasses import dataclass, field

RISK_LOW = "LOW"
RISK_MEDIUM = "MEDIUM"
RISK_HIGH = "HIGH"

EXEC_GATEWAY = "gateway"
EXEC_LOCAL_UI = "local_ui"

STATUS_IMPLEMENTED = "implemented"
STATUS_PLANNED = "planned"


@dataclass(frozen=True, slots=True)
class CommandSpec:
    name: str
    description: str
    arguments: tuple[str, ...] = ()
    risk: str = RISK_LOW
    endpoint: str | None = None
    handler: str = ""
    executor: str = EXEC_GATEWAY
    status: str = STATUS_IMPLEMENTED
    channels: frozenset[str] = field(default_factory=lambda: frozenset({"cli", "telegram"}))
    long_description: str = ""
    usage: str = ""
    flags: tuple[str, ...] = ()
    examples: tuple[str, ...] = ()
    category: str = ""
    emoji: str = ""
    read_only: bool = False
    idempotent: bool = False
    destructive: bool = False
    requires_approval: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "description": self.description,
            "arguments": list(self.arguments),
            "risk_level": self.risk,
            "endpoint": self.endpoint,
            "handler": self.handler,
            "executor": self.executor,
            "status": self.status,
            "channels": sorted(self.channels),
            "long_description": self.long_description,
            "usage": self.usage,
            "flags": list(self.flags),
            "examples": list(self.examples),
            "category": self.category,
            "emoji": self.emoji,
            "read_only": self.read_only,
            "idempotent": self.idempotent,
            "destructive": self.destructive,
            "requires_approval": self.requires_approval,
        }


_COMMANDS: list[CommandSpec] = [
    CommandSpec(
        name="help",
        description="Справка по командам",
        executor=EXEC_LOCAL_UI,
        handler="ui.help",
        long_description="Справка по командам CLI",
        usage="/help",
        category="ui",
        emoji="❓",
        read_only=True,
        idempotent=True,
    ),
    CommandSpec(
        name="exit",
        description="Выйти из интерфейса",
        executor=EXEC_LOCAL_UI,
        handler="ui.exit",
        channels=frozenset({"cli"}),
        long_description="Выйти из интерфейса",
        usage="/exit",
        examples=("/exit",),
        category="ui",
        emoji="🚪",
        read_only=True,
        idempotent=True,
    ),
    CommandSpec(
        name="reset",
        description="Завершить текущий диалог в этом чате",
        endpoint="POST /sessions/{session_id}/reset",
        executor=EXEC_GATEWAY,
        handler="session.reset",
        channels=frozenset({"telegram"}),
        long_description=(
            "Очищает историю диалога и активную задачу текущего чата. "
            "Аналог /exit для CLI, но не завершает процесс бота — он общий "
            "для всех чатов."
        ),
        usage="/reset",
        examples=("/reset",),
        category="ui",
        emoji="🔄",
        read_only=False,
        idempotent=True,
    ),
    CommandSpec(
        name="health",
        description="Проверка доступности Gateway",
        endpoint="GET /health",
        handler="gateway.health",
        long_description="Проверка доступности Gateway",
        usage="/health",
        examples=("/health",),
        category="system",
        emoji="❤️",
        read_only=True,
        idempotent=True,
    ),
    CommandSpec(
        name="commands",
        description="Список команд системы",
        endpoint="GET /commands",
        handler="gateway.commands",
        long_description="Список команд системы из реестра",
        usage="/commands",
        category="system",
        emoji="🧰",
        read_only=True,
        idempotent=True,
    ),
    CommandSpec(
        name="status",
        description="Статус задач (все или по ID)",
        arguments=("flow_id?",),
        endpoint="GET /flows",
        handler="gateway.flows.list",
        long_description="Статус задач: все или по ID",
        usage="/status [flow_id]",
        flags=("--all",),
        examples=("/status", "/status 4ab82f74"),
        category="tasks",
        emoji="📊",
        read_only=True,
        idempotent=True,
    ),
    CommandSpec(
        name="list",
        description="Список задач",
        endpoint="GET /flows",
        handler="gateway.flows.list",
        long_description="Список активных задач",
        usage="/list",
        examples=("/list",),
        category="tasks",
        emoji="📌",
        read_only=True,
        idempotent=True,
    ),
    CommandSpec(
        name="tasks",
        description="Список задач",
        endpoint="GET /flows",
        handler="gateway.flows.list",
        long_description="Список активных задач (алиас /list)",
        usage="/tasks",
        examples=("/tasks",),
        category="tasks",
        emoji="📌",
        read_only=True,
        idempotent=True,
    ),
    CommandSpec(
        name="get",
        description="Состояние задачи по ID",
        arguments=("flow_id",),
        endpoint="GET /flows/{flow_id}",
        handler="gateway.flows.get",
        long_description="Детали задачи по ID",
        usage="/get <flow_id>",
        examples=("/get 4ab82f74",),
        category="tasks",
        emoji="📄",
        read_only=True,
        idempotent=True,
    ),
    CommandSpec(
        name="cancel",
        description="Отменить задачу",
        arguments=("flow_id",),
        risk=RISK_MEDIUM,
        endpoint="POST /flows/{flow_id}/cancel",
        handler="gateway.flows.cancel",
        long_description="Отменить выполняющуюся задачу",
        usage="/cancel <flow_id>",
        examples=("/cancel 4ab82f74",),
        category="tasks",
        emoji="⛔",
        destructive=True,
        requires_approval=False,
        idempotent=False,
    ),
    CommandSpec(
        name="steer",
        description="Скорректировать выполняющуюся задачу",
        arguments=("flow_id", "text"),
        risk=RISK_MEDIUM,
        endpoint="POST /flows/{flow_id}/steer",
        handler="gateway.flows.steer",
        long_description="Скорректировать выполняющуюся задачу",
        usage="/steer <flow_id> <текст>",
        examples=("/steer 4ab82f74 ускорься",),
        category="tasks",
        emoji="🧭",
        destructive=False,
        requires_approval=False,
    ),
    CommandSpec(
        name="approvals",
        description="Список ожидающих одобрений",
        endpoint="GET /approvals",
        handler="gateway.approvals.list",
        long_description="Список ожидающих одобрений",
        usage="/approvals",
        examples=("/approvals",),
        category="approvals",
        emoji="🔐",
        read_only=True,
        idempotent=True,
    ),
    CommandSpec(
        name="approve",
        description="Одобрить операцию",
        arguments=("approval_id?",),
        risk=RISK_HIGH,
        endpoint="POST /approvals/{approval_id}/decision",
        handler="gateway.approvals.decide",
        long_description="Одобрить операцию (без ID — авто-выбор единственного)",
        usage="/approve [approval_id]",
        examples=("/approve", "/approve 7f3a9c"),
        category="approvals",
        emoji="✅",
        read_only=False,
        idempotent=True,
    ),
    CommandSpec(
        name="deny",
        description="Отклонить операцию",
        arguments=("approval_id?",),
        risk=RISK_HIGH,
        endpoint="POST /approvals/{approval_id}/decision",
        handler="gateway.approvals.decide",
        long_description="Отклонить операцию (без ID — авто-выбор единственного)",
        usage="/deny [approval_id]",
        examples=("/deny", "/deny 7f3a9c"),
        category="approvals",
        emoji="❌",
        read_only=False,
        idempotent=True,
    ),
    CommandSpec(
        name="session",
        description="Информация о сессии",
        arguments=("session_id",),
        endpoint="GET /sessions/{session_id}",
        handler="gateway.sessions.get",
        long_description="Информация о сессии",
        usage="/session <session_id>",
        examples=("/session cli-session",),
        category="context",
        emoji="💬",
        read_only=True,
        idempotent=True,
    ),
    CommandSpec(
        name="history",
        description="История переписки сессии",
        arguments=("session_id",),
        endpoint="GET /sessions/{session_id}/history",
        handler="gateway.sessions.history",
        long_description="История переписки сессии",
        usage="/history <session_id>",
        examples=("/history cli-session",),
        category="context",
        emoji="🕘",
        read_only=True,
        idempotent=True,
    ),
    CommandSpec(
        name="memory",
        description="Память агента (посмотреть/записать)",
        arguments=("text?",),
        risk=RISK_MEDIUM,
        endpoint="POST /api/v1/memory",
        handler="core.memory",
        long_description="Память агента: посмотреть или записать",
        usage="/memory [текст]",
        examples=("/memory", "/memory запомни: предпочитает краткость"),
        category="context",
        emoji="🧠",
        read_only=False,
        idempotent=True,
    ),
    CommandSpec(
        name="install",
        description="Установить инструмент в песочницу",
        arguments=("manager", "package"),
        risk=RISK_HIGH,
        handler="brain.command.install",
        long_description=(
            "Установить пакет/инструмент в изолированную песочницу "
            "(pip|npm|apt). Высокорисковые установки могут требовать /approve."
        ),
        usage="/install <pip|npm|apt> <пакет>",
        examples=("/install pip six",),
        category="tools",
        emoji="🔧",
        read_only=False,
        requires_approval=True,
    ),
    CommandSpec(
        name="image",
        description="Сгенерировать картинку (бесплатно, Pollinations.ai)",
        arguments=("prompt",),
        handler="brain.command.image",
        long_description=(
            "Бесплатная генерация изображения по описанию через Pollinations.ai "
            "(без API-ключа). Результат отправляется в Telegram."
        ),
        usage="/image <описание>",
        examples=("/image кот в шляпе",),
        category="tools",
        emoji="🎨",
        read_only=True,
    ),
    CommandSpec(
        name="tts",
        description="Отправить голосовое сообщение (TTS)",
        arguments=("text",),
        handler="brain.command.tts",
        long_description=(
            "Синтез речи (edge-tts, бесплатно, ru-RU) и отправка голосового "
            "сообщения в Telegram."
        ),
        usage="/tts <текст>",
        examples=("/tts Привет, как дела?",),
        category="tools",
        emoji="🎙",
        read_only=True,
    ),
    CommandSpec(
        name="web",
        description="Поиск в интернете",
        arguments=("query",),
        handler="brain.command.web",
        long_description="Поиск в интернете через DuckDuckGo Lite (без ключа).",
        usage="/web <запрос>",
        examples=("/web новости ИИ",),
        category="tools",
        emoji="🌐",
        read_only=True,
    ),
    CommandSpec(
        name="ollama",
        description="Управление локальным LLM (Ollama)",
        arguments=("action",),
        handler="brain.command.ollama",
        long_description=(
            "Управление локальной моделью Ollama: status, start, list, pull, switch."
        ),
        usage="/ollama <status|start|list|pull|switch> [модель]",
        examples=("/ollama status",),
        category="tools",
        emoji="🤖",
        read_only=False,
    ),
    CommandSpec(
        name="mcp",
        description="Управление MCP-серверами",
        arguments=("action",),
        risk=RISK_MEDIUM,
        handler="brain.command.mcp",
        long_description=(
            "Управление зарегистрированными MCP-серверами: "
            "/mcp list, /mcp add <name> <команда|url>, /mcp remove <name>."
        ),
        usage="/mcp list|add <name> <cmd|url>|remove <name>",
        examples=("/mcp list", "/mcp add gmail npx -y @gmail/server"),
        category="tools",
        emoji="🗂",
        read_only=False,
    ),
    CommandSpec(
        name="plugins",
        description="Управление плагинами",
        arguments=("action",),
        risk=RISK_MEDIUM,
        handler="brain.command.plugins",
        long_description=(
            "Управление плагинами: /plugins list, "
            "/plugins load <name>, /plugins unload <name>."
        ),
        usage="/plugins list|load <name>|unload <name>",
        examples=("/plugins list", "/plugins load my-plugin"),
        category="tools",
        emoji="🧩",
        read_only=False,
    ),
    CommandSpec(
        name="skills",
        description="Список установленных навыков",
        arguments=("action",),
        risk=RISK_MEDIUM,
        handler="brain.command.skills",
        long_description=(
            "Навыки Antigona: /skills list перечисляет установленные навыки."
        ),
        usage="/skills list",
        examples=("/skills list",),
        category="tools",
        emoji="🛠",
        read_only=False,
    ),
    CommandSpec(
        name="cli",
        description="Справка по CLI-командам",
        executor=EXEC_GATEWAY,
        handler="brain.command.cli",
        long_description="Справка по CLI-командам Antigona",
        usage="/cli",
        examples=("/cli",),
        category="ui",
        emoji="🖥",
        read_only=True,
        idempotent=True,
    ),
    CommandSpec(
        name="hermes",
        description="Hermes RCA: диагностика ошибок",
        executor=EXEC_GATEWAY,
        handler="brain.command.hermes",
        long_description="Hermes RCA — read-only root-cause analysis. /hermes last — последние диагнозы; /hermes trace <cid> — трасса по correlation_id; /hermes explain <error_id> — детальный разбор ошибки; /hermes evidence <rca_id> — evidence-цепочка; /hermes suggest-fix <rca_id> — предложенная ремедиация (данные).",
        usage="/hermes [last|trace <correlation_id>|explain <error_id>|evidence <rca_id>|suggest-fix <rca_id>]",
        examples=("/hermes last", "/hermes trace abc123", "/hermes explain err_abc", "/hermes evidence rca_1", "/hermes suggest-fix rca_1"),
        category="diagnostics",
        emoji="🤖",
        read_only=True,
        idempotent=True,
    ),
]


def command_registry() -> tuple[CommandSpec, ...]:
    return tuple(_COMMANDS)


def find_command(name: str) -> CommandSpec | None:
    normalized = name.strip().lower().lstrip("/")
    for spec in _COMMANDS:
        if spec.name == normalized:
            return spec
    return None


def commands_for_channel(channel: str) -> list[CommandSpec]:
    return [spec for spec in _COMMANDS if channel in spec.channels]


def registry_payload(channel: str = "cli") -> list[dict[str, object]]:
    return [spec.to_dict() for spec in commands_for_channel(channel)]
