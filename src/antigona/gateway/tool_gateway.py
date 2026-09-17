"""Tool Gateway — per-tool backend selection for Antigona's tools.

The gateway keeps one :class:`ToolConfig` per tool (``web``, ``image_gen``,
``tts``, ``browser``) describing which backend serves that tool and whether
calls are routed through the gateway at all. Configuration is persisted as
JSON in ``.antigona/gateway_config.json`` so switches survive a restart.

Usage::

    from antigona.gateway import get_gateway

    router = get_gateway()
    config, use_gateway = router.resolve("image_gen")
    if use_gateway:
        ...  # dispatch on config.backend
    else:
        ...  # call the tool directly, bypassing the gateway
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar

from antigona.core import paths

logger = logging.getLogger(__name__)

# ─── Paths ────────────────────────────────────────────────────────────────────

PROJECT_ROOT = paths.project_root()
CONFIG_DIR = paths.project_local_dir()
DEFAULT_CONFIG_PATH = CONFIG_DIR / "gateway_config.json"

# ─── Names ────────────────────────────────────────────────────────────────────


class ToolName(StrEnum):
    """Canonical tool identifiers understood by the gateway."""

    WEB = "web"
    IMAGE_GEN = "image_gen"
    TTS = "tts"
    BROWSER = "browser"


class Backend(StrEnum):
    """Backend identifiers that tools can be routed to."""

    DUCKDUCKGO = "duckduckgo"
    FIRECRAWL = "firecrawl"
    POLLINATIONS = "pollinations"
    OPENAI = "openai"
    EDGE_TTS = "edge-tts"
    PLAYWRIGHT = "playwright"


#: Backends accepted for each tool, in display order.
KNOWN_BACKENDS: dict[str, tuple[str, ...]] = {
    ToolName.WEB: (Backend.DUCKDUCKGO, Backend.FIRECRAWL),
    ToolName.IMAGE_GEN: (Backend.POLLINATIONS, Backend.OPENAI),
    ToolName.TTS: (Backend.OPENAI, Backend.EDGE_TTS),
    ToolName.BROWSER: (Backend.PLAYWRIGHT,),
}

#: Human aliases accepted on the command line -> canonical tool name.
TOOL_ALIASES: dict[str, str] = {
    "web": ToolName.WEB,
    "search": ToolName.WEB,
    "web_search": ToolName.WEB,
    "image": ToolName.IMAGE_GEN,
    "image_gen": ToolName.IMAGE_GEN,
    "imagegen": ToolName.IMAGE_GEN,
    "tts": ToolName.TTS,
    "voice": ToolName.TTS,
    "browser": ToolName.BROWSER,
}

#: Labels used when rendering the status table.
TOOL_LABELS: dict[str, str] = {
    ToolName.WEB: "🌐 Web",
    ToolName.IMAGE_GEN: "🖼 Image Gen",
    ToolName.TTS: "🔊 TTS",
    ToolName.BROWSER: "🌍 Browser",
}


class UnknownToolError(KeyError):
    """Raised when a tool name is not present in the gateway configuration."""


# ─── Per-tool config ──────────────────────────────────────────────────────────


@dataclass
class ToolConfig:
    """Configuration for a single tool.

    Attributes:
        backend: Backend identifier, e.g. ``"duckduckgo"`` or ``"pollinations"``.
        use_gateway: ``True`` routes calls through the gateway, ``False`` tells
            callers to take the direct path and ignore the backend selection.
    """

    backend: str
    use_gateway: bool = True

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation."""
        return {"backend": self.backend, "use_gateway": self.use_gateway}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ToolConfig:
        """Build a :class:`ToolConfig` from a plain dict."""
        return cls(
            backend=str(data.get("backend", "")),
            use_gateway=bool(data.get("use_gateway", True)),
        )


# ─── Whole-gateway config ─────────────────────────────────────────────────────


class ToolGatewayConfig:
    """Backend configuration for every gateway-managed tool.

    The instance is mutation-safe: every read/write of :attr:`tools` is guarded
    by an internal lock, so a Telegram handler and a worker can share one config.
    """

    DEFAULT_CONFIG: ClassVar[dict[str, ToolConfig]] = {
        ToolName.WEB: ToolConfig(backend=Backend.DUCKDUCKGO, use_gateway=True),
        ToolName.IMAGE_GEN: ToolConfig(backend=Backend.POLLINATIONS, use_gateway=True),
        ToolName.TTS: ToolConfig(backend=Backend.EDGE_TTS, use_gateway=True),
        ToolName.BROWSER: ToolConfig(backend=Backend.PLAYWRIGHT, use_gateway=True),
    }

    def __init__(self, tools: dict[str, ToolConfig] | None = None) -> None:
        self._lock = threading.Lock()
        self.tools: dict[str, ToolConfig] = (
            self.defaults() if tools is None else {k: replace(v) for k, v in tools.items()}
        )

    # ── Defaults ──────────────────────────────────────────────────────────

    @classmethod
    def defaults(cls) -> dict[str, ToolConfig]:
        """Return a fresh copy of :attr:`DEFAULT_CONFIG`."""
        return {name: replace(cfg) for name, cfg in cls.DEFAULT_CONFIG.items()}

    def reset_to_defaults(self) -> None:
        """Restore every tool to its default backend and gateway flag."""
        with self._lock:
            self.tools = self.defaults()

    # ── Access ────────────────────────────────────────────────────────────

    def get_tool(self, name: str) -> ToolConfig:
        """Return the config for *name*.

        Args:
            name: Canonical tool name (``"web"``, ``"image_gen"``, ...).

        Raises:
            UnknownToolError: If the tool is not configured.
        """
        with self._lock:
            config = self.tools.get(name)
        if config is None:
            raise UnknownToolError(name)
        return config

    def set_backend(self, name: str, backend: str) -> None:
        """Point tool *name* at *backend*.

        Args:
            name: Canonical tool name.
            backend: Backend identifier; validated against :data:`KNOWN_BACKENDS`
                when the tool has a known backend list.

        Raises:
            UnknownToolError: If the tool is not configured.
            ValueError: If the backend is not supported for that tool.
        """
        known = KNOWN_BACKENDS.get(name)
        if known is not None and backend not in known:
            raise ValueError(
                f"Backend '{backend}' is not supported for tool '{name}'. "
                f"Supported: {', '.join(known)}"
            )
        with self._lock:
            config = self.tools.get(name)
            if config is None:
                raise UnknownToolError(name)
            self.tools[name] = replace(config, backend=backend)

    def set_use_gateway(self, name: str, flag: bool) -> None:
        """Enable or disable gateway routing for tool *name*.

        Raises:
            UnknownToolError: If the tool is not configured.
        """
        with self._lock:
            config = self.tools.get(name)
            if config is None:
                raise UnknownToolError(name)
            self.tools[name] = replace(config, use_gateway=flag)

    # ── Serialization ─────────────────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable snapshot of the whole config."""
        with self._lock:
            return {"tools": {name: cfg.to_dict() for name, cfg in self.tools.items()}}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ToolGatewayConfig:
        """Build a config from a dict, filling missing tools with defaults."""
        tools = cls.defaults()
        raw_tools = data.get("tools", {})
        if isinstance(raw_tools, dict):
            for name, raw in raw_tools.items():
                if isinstance(raw, dict):
                    tools[str(name)] = ToolConfig.from_dict(raw)
                else:
                    logger.warning("Ignoring malformed gateway entry for %r", name)
        else:
            logger.warning("Gateway config has no 'tools' mapping; using defaults.")
        return cls(tools=tools)

    def save(self, path: str | Path = DEFAULT_CONFIG_PATH) -> None:
        """Write the config to *path* as JSON, creating parent dirs."""
        dest = Path(path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(
            json.dumps(self.to_dict(), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        logger.debug("Tool gateway config saved: %s", dest)

    @classmethod
    def load(cls, path: str | Path = DEFAULT_CONFIG_PATH) -> ToolGatewayConfig:
        """Read the config from *path*.

        A missing or unreadable file is not an error: defaults are returned so
        the gateway always has a usable configuration.
        """
        src = Path(path)
        try:
            raw = json.loads(src.read_text(encoding="utf-8"))
        except FileNotFoundError:
            logger.debug("No tool gateway config at %s; using defaults.", src)
            return cls()
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Unreadable tool gateway config %s: %s", src, exc)
            return cls()
        if not isinstance(raw, dict):
            logger.warning("Tool gateway config %s is not an object; using defaults.", src)
            return cls()
        return cls.from_dict(raw)


# ─── Router ───────────────────────────────────────────────────────────────────


class GatewayRouter:
    """Resolve which backend serves a tool, and persist changes to disk."""

    def __init__(
        self,
        config: ToolGatewayConfig | None = None,
        config_path: str | Path = DEFAULT_CONFIG_PATH,
    ) -> None:
        self.config_path = Path(config_path)
        self.config = config if config is not None else ToolGatewayConfig.load(self.config_path)

    # ── Resolution ────────────────────────────────────────────────────────

    def resolve(self, tool_name: str) -> tuple[ToolConfig, bool]:
        """Return ``(config, use_gateway)`` for *tool_name*.

        The returned config is a copy, so callers cannot mutate gateway state by
        accident. When ``use_gateway`` is ``False`` the caller should ignore the
        backend selection and take the tool's direct path.

        Raises:
            UnknownToolError: If the tool is not configured.
        """
        config = replace(self.config.get_tool(tool_name))
        return config, config.use_gateway

    def list_tools(self) -> dict[str, dict[str, Any]]:
        """Return ``{tool_name: {backend, use_gateway, status}}`` for display."""
        snapshot = self.config.to_dict()["tools"]
        return {
            name: {
                "backend": entry["backend"],
                "use_gateway": entry["use_gateway"],
                "status": "on" if entry["use_gateway"] else "off",
            }
            for name, entry in snapshot.items()
        }

    # ── Mutation (persisted) ──────────────────────────────────────────────

    def set_backend(self, tool_name: str, backend: str) -> None:
        """Switch *tool_name* to *backend* and persist the change."""
        self.config.set_backend(tool_name, backend)
        self.save()

    def set_use_gateway(self, tool_name: str, flag: bool) -> None:
        """Toggle gateway routing for *tool_name* and persist the change."""
        self.config.set_use_gateway(tool_name, flag)
        self.save()

    def reset(self) -> None:
        """Restore defaults for every tool and persist them."""
        self.config.reset_to_defaults()
        self.save()

    def save(self) -> None:
        """Persist the current config to :attr:`config_path`."""
        self.config.save(self.config_path)


# ─── Singleton ────────────────────────────────────────────────────────────────

_gateway: GatewayRouter | None = None
_gateway_lock = threading.Lock()


def get_gateway() -> GatewayRouter:
    """Return the process-wide :class:`GatewayRouter`, creating it on first use."""
    global _gateway
    with _gateway_lock:
        if _gateway is None:
            _gateway = GatewayRouter()
        return _gateway


def reset_gateway() -> None:
    """Drop the cached singleton (used by tests and after a config reload)."""
    global _gateway
    with _gateway_lock:
        _gateway = None


# ─── Display / command parsing ────────────────────────────────────────────────

_TRUE_WORDS = frozenset({"true", "on", "yes", "1", "да"})
_FALSE_WORDS = frozenset({"false", "off", "no", "0", "нет"})


def _parse_flag(value: str) -> bool | None:
    """Parse a boolean word; return ``None`` when it is not recognised."""
    lowered = value.strip().lower()
    if lowered in _TRUE_WORDS:
        return True
    if lowered in _FALSE_WORDS:
        return False
    return None


def resolve_tool_alias(name: str) -> str | None:
    """Map a user-typed tool name to its canonical name, or ``None``."""
    return TOOL_ALIASES.get(name.strip().lower())


def format_status_table(tools: dict[str, dict[str, Any]]) -> str:
    """Render :meth:`GatewayRouter.list_tools` output as a fixed-width table."""
    lines = ["━━━ Tool Gateway ━━━"]
    order = [name for name in TOOL_LABELS if name in tools]
    order += [name for name in tools if name not in TOOL_LABELS]
    label_width = max((len(TOOL_LABELS.get(n, n)) for n in order), default=0)
    backend_width = max((len(str(tools[n]["backend"])) for n in order), default=0)
    for name in order:
        entry = tools[name]
        label = TOOL_LABELS.get(name, name).ljust(label_width)
        backend = str(entry["backend"]).ljust(backend_width)
        state = "✅ Gateway on" if entry["use_gateway"] else "❌ Gateway off"
        lines.append(f"{label} │ {backend} │ {state}")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━")
    return "\n".join(lines)


def format_tool_help() -> str:
    """Return usage text for the ``/tool-gateway`` command."""
    backends = "\n".join(
        f"  {TOOL_LABELS.get(tool, tool)}: {', '.join(names)}"
        for tool, names in KNOWN_BACKENDS.items()
    )
    return (
        "🔀 /tool-gateway — маршрутизация инструментов\n\n"
        "  /tool-gateway status — статус всех инструментов\n"
        "  /tool-gateway <tool> — конфигурация одного инструмента\n"
        "  /tool-gateway <tool> backend <name> — переключить бэкенд\n"
        "  /tool-gateway <tool> use_gateway true/false — вкл/выкл шлюз\n"
        "  /tool-gateway reset — вернуть настройки по умолчанию\n\n"
        f"Инструменты: {', '.join(TOOL_LABELS)}\n"
        f"Бэкенды:\n{backends}"
    )


def _format_single_tool(name: str, entry: dict[str, Any]) -> str:
    """Render the config of one tool as a short block."""
    state = "✅ включён" if entry["use_gateway"] else "❌ выключен"
    known = KNOWN_BACKENDS.get(name, ())
    lines = [
        f"{TOOL_LABELS.get(name, name)}",
        f"  backend: {entry['backend']}",
        f"  gateway: {state}",
    ]
    if known:
        lines.append(f"  доступно: {', '.join(known)}")
    return "\n".join(lines)


def handle_tool_gateway_command(
    args: list[str],
    router: GatewayRouter | None = None,
) -> str:
    """Execute a ``/tool-gateway`` subcommand and return the reply text.

    Args:
        args: Command arguments with the command name already stripped, e.g.
            ``["web", "backend", "duckduckgo"]``.
        router: Router to act on; defaults to the process singleton.

    Returns:
        Reply text ready to send to the user.
    """
    gateway = router if router is not None else get_gateway()
    tokens = [a for a in args if a.strip()]

    if not tokens or tokens[0].lower() == "status":
        return format_status_table(gateway.list_tools())

    head = tokens[0].lower()

    if head in ("help", "?"):
        return format_tool_help()

    if head == "reset":
        gateway.reset()
        return "♻️ Конфигурация шлюза сброшена.\n\n" + format_status_table(gateway.list_tools())

    tool = resolve_tool_alias(head)
    if tool is None:
        available = ", ".join(TOOL_LABELS)
        return f"❌ Неизвестный инструмент: {head}\nДоступные: {available}"

    tools = gateway.list_tools()
    if tool not in tools:
        return f"❌ Инструмент '{tool}' не сконфигурирован."

    if len(tokens) == 1:
        return _format_single_tool(tool, tools[tool])

    action = tokens[1].lower()

    if action == "backend":
        if len(tokens) < 3:
            known = ", ".join(KNOWN_BACKENDS.get(tool, ()))
            return f"❌ Укажите бэкенд: /tool-gateway {head} backend <name>\nДоступные: {known}"
        backend = tokens[2]
        try:
            gateway.set_backend(tool, backend)
        except ValueError as exc:
            return f"❌ {exc}"
        return f"✅ {TOOL_LABELS.get(tool, tool)} → бэкенд '{backend}'."

    if action in ("use_gateway", "use-gateway", "gateway"):
        if len(tokens) < 3:
            return f"❌ Укажите значение: /tool-gateway {head} use_gateway true|false"
        flag = _parse_flag(tokens[2])
        if flag is None:
            return f"❌ Не понял значение '{tokens[2]}'. Ожидается true или false."
        gateway.set_use_gateway(tool, flag)
        state = "включён" if flag else "выключен"
        return f"✅ Шлюз для {TOOL_LABELS.get(tool, tool)} {state}."

    return f"❌ Неизвестное действие: {action}\n\n{format_tool_help()}"
