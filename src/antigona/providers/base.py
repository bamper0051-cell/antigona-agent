"""Abstract provider interface for LLM response generation."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class BaseProvider(ABC):
    """Abstract base class for LLM providers.

    Every provider implements generate() which takes a conversation
    history and optional context and returns a text response string.
    """

    name: str = "base"

    @abstractmethod
    def generate(self, messages: list[dict[str, str]], context: dict[str, Any] | None = None) -> str:
        """Generate a response from the provider.

        Args:
            messages: Conversation history in OpenAI-compatible format
                ([{"role": "user"|"assistant"|"system", "content": "..."}, ...]).
            context: Optional provider-specific context (temperature, max_tokens, etc.).

        Returns:
            The generated response text.

        Raises:
            ProviderError: If generation fails.
        """
        ...


class ProviderError(Exception):
    """Base exception for provider failures."""
