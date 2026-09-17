"""Hooks system -- event-driven callbacks with pattern matching.

Inspired by prior work, adapted for Antigona's typed event bus.

A **Hook** is a named callback registered for one or more event patterns::

    hook_registry.register(Hook(
        name="log_all_messages",
        event_pattern="message:*",
        handler=my_async_handler,
    ))

Built-in event names (used as strings, mapped from event types):

    - ``message:received``   -- fired on ``MessageReceived``
    - ``command:<name>``     -- fired when a command is detected
    - ``agent:start``        -- agent session start
    - ``agent:end``          -- agent session end
    - ``agent:step``         -- single agent reasoning step
    - ``cron:tick``          -- cron scheduler tick
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from fnmatch import fnmatch
from typing import Any

from antigona.events.event_types import BaseEvent

logger = logging.getLogger(__name__)

# Type alias: async handler taking event context
HookHandler = Callable[[str, dict[str, Any]], Awaitable[None]]


class Hook:
    """A single hook registration.

    Attributes:
        name: Unique human-readable name for this hook.
        event_pattern: Glob-style pattern to match event names, e.g. ``message:*``.
        handler: Async callable ``(event_name, context_dict) -> None``.
        enabled: Whether the hook is active.
        description: Optional human-readable description.
    """

    def __init__(
        self,
        name: str,
        event_pattern: str,
        handler: HookHandler,
        *,
        enabled: bool = True,
        description: str = "",
    ) -> None:
        self.name = name
        self.event_pattern = event_pattern
        self.handler = handler
        self.enabled = enabled
        self.description = description or f"Hook '{name}' for '{event_pattern}'"

    def matches(self, event_name: str) -> bool:
        """Check if *event_name* matches this hook's pattern (glob)."""
        if not self.enabled:
            return False
        return fnmatch(event_name, self.event_pattern)

    async def fire(self, event_name: str, context: dict[str, Any]) -> None:
        """Invoke the handler if this hook matches *event_name*."""
        if not self.enabled:
            return
        try:
            await self.handler(event_name, context)
        except Exception:
            logger.exception(
                "Hook '%s' handler failed for event '%s'", self.name, event_name
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "event_pattern": self.event_pattern,
            "enabled": self.enabled,
            "description": self.description,
        }


class HookRegistry:
    """Central registry of hooks.

    Thread-safe for async usage (no shared mutable state between coroutines
    beyond the registry dict itself, which is only modified by register/unregister).
    """

    def __init__(self) -> None:
        self._hooks: dict[str, Hook] = {}

    def register(self, hook: Hook) -> None:
        """Register a hook (replaces existing by name)."""
        self._hooks[hook.name] = hook
        logger.info("Hook registered: '%s' -> '%s'", hook.name, hook.event_pattern)

    def unregister(self, name: str) -> bool:
        """Remove a hook by name. Returns ``True`` if found."""
        if name in self._hooks:
            del self._hooks[name]
            logger.info("Hook unregistered: '%s'", name)
            return True
        return False

    def get(self, name: str) -> Hook | None:
        """Look up a hook by name."""
        return self._hooks.get(name)

    def list_hooks(self) -> list[Hook]:
        """Return all registered hooks."""
        return list(self._hooks.values())

    def list_enabled(self) -> list[Hook]:
        """Return only enabled hooks."""
        return [h for h in self._hooks.values() if h.enabled]

    async def fire(self, event_name: str, context: dict[str, Any] | None = None) -> None:
        """Fire all matching hooks for *event_name*.

        Hooks run concurrently. Errors are logged individually.
        Does NOT raise.
        """
        import asyncio

        context = context or {}
        tasks = []
        for hook in self._hooks.values():
            if hook.matches(event_name):
                tasks.append(hook.fire(event_name, context))

        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for i, res in enumerate(results):
                if isinstance(res, BaseException):
                    logger.error(
                        "Hook fire error for '%s' (hook index %d): %s",
                        event_name, i, res,
                    )

    def count(self) -> int:
        return len(self._hooks)

    def clear(self) -> None:
        """Remove all hooks."""
        self._hooks.clear()


# ── Module-level singleton ────────────────────────────────────────────────

_HOOK_REGISTRY: HookRegistry | None = None


def get_hook_registry() -> HookRegistry:
    """Get the module-level singleton HookRegistry."""
    global _HOOK_REGISTRY
    if _HOOK_REGISTRY is None:
        _HOOK_REGISTRY = HookRegistry()
    return _HOOK_REGISTRY


# ── Built-in event name resolver ──────────────────────────────────────────

# Maps event type names (as stored in event_types) to hook event names
EVENT_NAME_MAP: dict[str, str] = {
    "MessageReceived": "message:received",
    "IntentClassified": "agent:step",       # every classified intent is a step
    "ConversationReply": "message:reply",
    "TaskCreated": "agent:start",
    "TaskCompleted": "agent:end",
    "ToolExecuted": "agent:step",
    "ErrorOccurred": "error:occurred",
    "CancelRequested": "cancel:requested",
    "Cancelled": "cancel:confirmed",
}


def event_name_from_type(event_type_name: str) -> str:
    """Convert an event type class name to a hook event name.

    Falls back to the original name if not mapped.
    """
    return EVENT_NAME_MAP.get(event_type_name, event_type_name.lower())


async def fire_hooks_for_event(
    registry: HookRegistry,
    event: BaseEvent,
    extra_context: dict[str, Any] | None = None,
) -> None:
    """Convenience: fire all hooks matching an ``BaseEvent`` instance."""
    event_name = event_name_from_type(type(event).__name__)
    context: dict[str, Any] = {
        "correlation_id": event.correlation_id,
        "timestamp": event.timestamp,
        "source": event.source,
        # Flatten dataclass fields for convenience
        **{
            k: v
            for k, v in event.__dict__.items()
            if not k.startswith("_")
        },
    }
    if extra_context:
        context.update(extra_context)
    await registry.fire(event_name, context)
