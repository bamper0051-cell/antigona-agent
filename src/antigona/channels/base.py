"""Platform Adapter ABC — abstract interface for all communication channels.

Every transport (Telegram, CLI, File, etc.) implements ``BasePlatformAdapter``.
Adapters are registered as platform plugins and provide a uniform API for:

  - Connecting / disconnecting
  - Sending text / media
  - Typing indicators
  - Chat / user metadata

Usage::

    class MyAdapter(BasePlatformAdapter, name="my_channel"):
        ...
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar, TextIO

# ─── Transport registry ────────────────────────────────────────────────────────


class TransportRegistryError(Exception):
    """Raised when transport registration fails."""


_TRANSPORT_REGISTRY: dict[str, type[BasePlatformAdapter]] = {}


def register_transport(name: str) -> Any:
    """Decorator that registers a transport class by name.

    Usage::

        @register_transport("telegram")
        class TelegramAdapter(BasePlatformAdapter):
            ...
    """

    def decorator(cls: type[BasePlatformAdapter]) -> type[BasePlatformAdapter]:
        if name in _TRANSPORT_REGISTRY:
            raise TransportRegistryError(
                f"Transport '{name}' is already registered by {_TRANSPORT_REGISTRY[name]}"
            )
        _TRANSPORT_REGISTRY[name] = cls
        return cls

    return decorator


def get_transport(name: str) -> type[BasePlatformAdapter]:
    """Get a registered transport class by name.

    Raises:
        KeyError: If the transport is not registered.
    """
    if name not in _TRANSPORT_REGISTRY:
        raise KeyError(
            f"Unknown transport '{name}'. "
            f"Registered: {', '.join(sorted(_TRANSPORT_REGISTRY))}"
        )
    return _TRANSPORT_REGISTRY[name]


def list_transports() -> list[str]:
    """List all registered transport names."""
    return list(_TRANSPORT_REGISTRY.keys())


# ─── Chat / user info models ──────────────────────────────────────────────────


@dataclass
class ChatInfo:
    """Metadata about a chat or conversation.

    Attributes:
        chat_id: Unique identifier for the chat.
        title: Human-readable title or name.
        type: Chat type (``"private"``, ``"group"``, ``"channel"``, etc.).
        platform: Platform name (``"telegram"``, ``"cli"``, ``"file"``).
        metadata: Any additional platform-specific fields.
    """

    chat_id: str
    title: str = ""
    type: str = "private"
    platform: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class UserInfo:
    """Metadata about a user.

    Attributes:
        user_id: Unique identifier.
        username: Display name or handle.
        first_name: Given name.
        last_name: Family name.
        metadata: Any additional platform-specific fields.
    """

    user_id: str
    username: str = ""
    first_name: str = ""
    last_name: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


# ─── Message envelope ─────────────────────────────────────────────────────────


@dataclass
class OutgoingMessage:
    """A message to send through a platform adapter.

    Attributes:
        text: The text content.
        chat_id: Target chat/conversation ID.
        reply_to: Optional message ID to reply to.
        attachments: Optional list of file paths or URLs.
        parse_mode: Optional parse mode (``"html"``, ``"markdown"``).
        metadata: Any additional platform-specific fields.
    """

    text: str = ""
    chat_id: str = ""
    reply_to: str | None = None
    attachments: list[str] = field(default_factory=list)
    parse_mode: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


# ─── Platform Adapter ABC ──────────────────────────────────────────────────────


class BasePlatformAdapter(ABC):
    """Abstract base class for all platform/transport adapters.

    Every adapter must implement:

      * ``connect()`` — establish connection to the platform.
      * ``disconnect()`` — tear down the connection.
      * ``send()`` — send a message.
      * ``send_typing()`` — show typing indicator.
      * ``get_chat_info()`` — fetch chat metadata.

    Class variables (set via subclass or override):

      * ``name`` — a unique short name like ``"telegram"``.
    """

    #: Human-readable platform name.  Subclasses should override.
    name: ClassVar[str] = "base"

    # ── Lifecycle ──────────────────────────────────────────────────────────

    @abstractmethod
    async def connect(self) -> bool:
        """Establish the connection to the platform.

        Returns:
            True on success, False on failure.
        """
        ...

    @abstractmethod
    async def disconnect(self) -> bool:
        """Tear down the connection gracefully.

        Returns:
            True on success, False on failure.
        """
        ...

    @property
    def connected(self) -> bool:
        """Whether the adapter is currently connected.

        Subclasses may override with a more precise check.
        """
        return False

    # ── I/O ────────────────────────────────────────────────────────────────

    @abstractmethod
    async def send(self, message: OutgoingMessage) -> bool:
        """Send a message through the platform.

        Args:
            message: The message to send (text, attachments, etc.).

        Returns:
            True if the message was sent successfully.
        """
        ...

    @abstractmethod
    async def send_typing(self, chat_id: str) -> bool:
        """Show a typing / composing indicator.

        Args:
            chat_id: The target chat.

        Returns:
            True if the indicator was shown.
        """
        ...

    # ── Metadata ───────────────────────────────────────────────────────────

    @abstractmethod
    async def get_chat_info(self, chat_id: str) -> ChatInfo:
        """Fetch metadata about a chat.

        Args:
            chat_id: The chat identifier.

        Returns:
            A *ChatInfo* instance.
        """
        ...

    async def get_user_info(self, user_id: str) -> UserInfo:
        """Fetch metadata about a user.

        The default implementation returns a minimal *UserInfo*.
        Subclasses may override with richer lookups.
        """
        return UserInfo(user_id=user_id)


# ═══════════════════════════════════════════════════════════════════════════════
# Built-in adapters
# ═══════════════════════════════════════════════════════════════════════════════


# ── CLI adapter ────────────────────────────────────────────────────────────────


@register_transport("cli")
class CLIAdapter(BasePlatformAdapter):
    """Simple adapter that prints to stdout / stderr.

    Useful for local testing and CLI mode.
    """

    name = "cli"

    def __init__(self) -> None:
        self._connected = False

    async def connect(self) -> bool:
        self._connected = True
        return True

    async def disconnect(self) -> bool:
        self._connected = False
        return True

    @property
    def connected(self) -> bool:
        return self._connected

    async def send(self, message: OutgoingMessage) -> bool:
        print(f"[{message.chat_id or 'stdout'}] {message.text}")
        for att in message.attachments:
            print(f"  [attachment] {att}")
        return True

    async def send_typing(self, chat_id: str) -> bool:
        return True  # no-op for CLI

    async def get_chat_info(self, chat_id: str) -> ChatInfo:
        return ChatInfo(chat_id=chat_id, title="CLI", type="private", platform="cli")


# ── File adapter ───────────────────────────────────────────────────────────────


@register_transport("file")
class FileAdapter(BasePlatformAdapter):
    """Adapter that writes messages to a log file.

    Useful for headless archiving / logging.
    """

    name = "file"

    def __init__(self, log_path: str | None = None) -> None:
        if log_path is None:
            from antigona.core import paths
            self.log_path = str(paths.channel_log_file())
        else:
            self.log_path = log_path
        self._connected = False
        self._file: TextIO | None = None

    async def connect(self) -> bool:
        import os
        _nf = getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(self.log_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | _nf, 0o600)
        self._file = open(fd, "a")  # noqa: SIM115
        self._connected = True
        return True

    async def disconnect(self) -> bool:
        if self._file:
            self._file.close()
            self._file = None
        self._connected = False
        return True

    @property
    def connected(self) -> bool:
        return self._connected

    async def send(self, message: OutgoingMessage) -> bool:
        if not self._file:
            return False
        import datetime

        ts = datetime.datetime.now(datetime.UTC).isoformat()
        line = f"[{ts}] [{message.chat_id}] {message.text}\n"
        self._file.write(line)
        for att in message.attachments:
            self._file.write(f"[{ts}]   [attachment] {att}\n")
        self._file.flush()
        return True

    async def send_typing(self, chat_id: str) -> bool:
        return True

    async def get_chat_info(self, chat_id: str) -> ChatInfo:
        return ChatInfo(
            chat_id=chat_id, title=f"File-{chat_id}", type="private", platform="file"
        )


# ── Telegram adapter (wraps existing bot) ──────────────────────────────────────


@register_transport("telegram")
class TelegramAdapter(BasePlatformAdapter):
    """Adapter that wraps the existing aiogram-based Telegram bot.

    This adapter expects a running ``Dispatcher`` instance.  It provides
    the standard ``BasePlatformAdapter`` interface for the API server and
    other programmatic integrations.
    """

    name = "telegram"

    def __init__(self, bot_instance: Any | None = None, dispatcher: Any | None = None) -> None:
        """Initialize with an optional existing bot + dispatcher.

        Args:
            bot_instance: An *aiogram.Bot* instance (or similar).
            dispatcher: An *aiogram.Dispatcher* instance (or similar).
        """
        self._bot = bot_instance
        self._dp = dispatcher
        self._connected = False

    async def connect(self) -> bool:
        if self._bot:
            self._connected = True
        return self._connected

    async def disconnect(self) -> bool:
        if self._bot:
            session = getattr(self._bot, "session", None)
            if session:
                await session.close()
        self._connected = False
        return True

    @property
    def connected(self) -> bool:
        return self._connected and self._bot is not None

    async def send(self, message: OutgoingMessage) -> bool:
        if not self._bot:
            return False
        try:
            text = message.text
            chat_id = message.chat_id
            if message.attachments:
                from aiogram.types import FSInputFile

                for path in message.attachments:
                    await self._bot.send_document(chat_id=chat_id, document=FSInputFile(path))
            if text:
                await self._bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    parse_mode=message.parse_mode,
                    disable_web_page_preview=True,
                )
            return True
        except Exception:
            return False

    async def send_typing(self, chat_id: str) -> bool:
        if not self._bot:
            return False
        try:
            from aiogram.enums import ChatAction

            await self._bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
            return True
        except Exception:
            return False

    async def get_chat_info(self, chat_id: str) -> ChatInfo:
        if not self._bot:
            return ChatInfo(chat_id=chat_id, platform="telegram")
        try:
            chat = await self._bot.get_chat(chat_id=chat_id)
            return ChatInfo(
                chat_id=str(chat.id),
                title=chat.title or chat.first_name or "",
                type=chat.type.value if hasattr(chat.type, "value") else str(chat.type),
                platform="telegram",
            )
        except Exception:
            return ChatInfo(chat_id=chat_id, platform="telegram")
