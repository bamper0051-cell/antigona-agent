from __future__ import annotations

from typing import TYPE_CHECKING

from .factory import get_adapter
from .readback import DeliveryOutcome, adapter_dispatch_outcome

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

    def deliver_outcome(
        self, channel_name: str, event: ProgressEvent, idempotency_key: str
    ) -> DeliveryOutcome:
        """Dispatch and return transmission plus the provider read-back level.

        The result keeps the legacy boolean contract on ``.transmitted`` and, for
        adapters that support it, carries the provider message identifier and the
        read-back status (see :mod:`antigona.delivery.readback`).
        """
        adapter = self.get_adapter(channel_name)
        return adapter_dispatch_outcome(adapter, event, idempotency_key)

    def deliver(self, channel_name: str, event: ProgressEvent, idempotency_key: str) -> bool:
        """``True`` when the channel adapter really transmitted, ``False`` when the
        dispatch was only simulated (mock runtime)."""
        return self.deliver_outcome(channel_name, event, idempotency_key).transmitted
