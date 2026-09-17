from __future__ import annotations

from typing import TYPE_CHECKING

from .factory import get_adapter

if TYPE_CHECKING:
    from antigona.config import Settings
    from antigona.delivery.adapter import DeliveryAdapter, ProgressEvent


class Router:
    """Routes delivery events to the appropriate channel adapter using lazy caching."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._cache: dict[str, DeliveryAdapter] = {}

    def get_adapter(self, channel_name: str) -> DeliveryAdapter:
        clean_name = (channel_name or "progress").strip().lower()
        if clean_name not in self._cache:
            adapter = get_adapter(clean_name, self.settings)
            self._cache[clean_name] = adapter
        return self._cache[clean_name]

    def deliver(self, channel_name: str, event: ProgressEvent, idempotency_key: str) -> bool:
        """``True`` when the channel adapter really transmitted, ``False`` when the
        dispatch was only simulated (mock runtime)."""
        adapter = self.get_adapter(channel_name)
        return adapter.deliver(event, idempotency_key)
