"""Mock provider for offline testing — returns pre-configured responses."""

from __future__ import annotations

from typing import Any

from antigona.providers.base import BaseProvider


class MockProvider(BaseProvider):
    """Provider that returns pre-configured mock responses.

    Useful for testing conversation flows without network access.
    Cycles through answers if more messages are sent than pre-configured responses.
    """

    name: str = "mock"

    def __init__(self, responses: list[str] | None = None) -> None:
        """Initialize with optional pre-configured responses.

        Args:
            responses: List of response strings to return in order.
                If None, uses a sensible default set.
        """
        self._responses: list[str] = responses if responses is not None else [
            "🤖 Это ответ MockProvider. Gateway недоступен, использую встроенный провайдер.",
            "📡 Сеть недоступна, но я всё ещё работаю. Чем могу помочь?",
            "💡 Режим offline — MockProvider активен. Ваш запрос обрабатывается локально.",
        ]
        self._call_count: int = 0

    def generate(self, messages: list[dict[str, str]], context: dict[str, Any] | None = None) -> str:
        """Return the next mock response, cycling through the response list."""
        if not self._responses:
            return ""
        idx = self._call_count % len(self._responses)
        self._call_count += 1
        return self._responses[idx]

    @property
    def call_count(self) -> int:
        """Number of times generate() has been called."""
        return self._call_count

    def reset(self) -> None:
        """Reset the call counter (for test isolation)."""
        self._call_count = 0
