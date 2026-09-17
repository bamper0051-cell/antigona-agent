"""Hooks package — event-driven plugin system.

Hooks allow registering callbacks that fire on named event patterns
(``message:received``, ``command:*``, ``agent:start``, etc.).
"""

from antigona.hooks.hooks import Hook, HookRegistry, get_hook_registry

__all__ = [
    "Hook",
    "HookRegistry",
    "get_hook_registry",
]
