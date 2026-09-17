"""Provider registry — register, select, and switch providers by name."""

from __future__ import annotations

from typing import Any

from antigona.providers.base import BaseProvider


class ProviderRegistryError(Exception):
    """Raised on registry operations with unknown/non-registered providers."""


class ProviderRegistry:
    """Registry for named providers with dynamic selection and switching.

    Typical usage::

        registry = ProviderRegistry()
        registry.register(MockProvider(), name="mock")
        registry.register(OpenAICompatibleProvider(...), name="openai")
        registry.select("mock")
        reply = registry.generate([{"role": "user", "content": "hello"}])
    """

    def __init__(self) -> None:
        self._providers: dict[str, BaseProvider] = {}
        self._active_name: str | None = None

    # ─── Registration ───────────────────────────────────────────────────────

    def register(self, provider: BaseProvider, name: str | None = None) -> str:
        """Register a provider under a given name.

        Args:
            provider: The provider instance to register.
            name: Alias to register under. If None, uses ``provider.name``.

        Returns:
            The name under which the provider was registered.
        """
        key = name or provider.name
        self._providers[key] = provider
        # Auto-select if this is the first registered provider.
        if self._active_name is None:
            self._active_name = key
        return key

    def unregister(self, name: str) -> None:
        """Remove a provider from the registry.

        Raises:
            ProviderRegistryError: If the name is not registered.
        """
        if name not in self._providers:
            raise ProviderRegistryError(f"Provider {name!r} is not registered")
        del self._providers[name]
        if self._active_name == name:
            # Re-select the first available provider, if any.
            self._active_name = next(iter(self._providers), None)

    # ─── Selection ──────────────────────────────────────────────────────────

    def select(self, name: str) -> None:
        """Switch the active provider.

        Args:
            name: Name of a previously registered provider.

        Raises:
            ProviderRegistryError: If the name is not registered.
        """
        if name not in self._providers:
            raise ProviderRegistryError(
                f"Provider {name!r} not found. Registered: {list(self._providers)}"
            )
        self._active_name = name

    @property
    def active_name(self) -> str | None:
        """Name of the currently active provider, or None if none registered."""
        return self._active_name

    @property
    def active(self) -> BaseProvider | None:
        """Currently active provider instance, or None if none registered."""
        if self._active_name is None:
            return None
        return self._providers.get(self._active_name)

    # ─── Listing ────────────────────────────────────────────────────────────

    def list_providers(self) -> dict[str, str]:
        """Return a dict of {name: provider_type} for all registered providers."""
        return {name: type(prov).__name__ for name, prov in self._providers.items()}

    # ─── Generation ─────────────────────────────────────────────────────────

    def generate(self, messages: list[dict[str, str]], context: dict[str, Any] | None = None) -> str:
        """Generate a response using the currently active provider.

        Args:
            messages: Conversation history in OpenAI-compatible format.
            context: Optional provider-specific context.

        Returns:
            Generated reply text.

        Raises:
            ProviderRegistryError: If no provider is registered or selected.
        """
        provider = self.active
        if provider is None:
            raise ProviderRegistryError("No provider registered or selected")
        return provider.generate(messages, context=context)
