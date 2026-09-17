"""Context Compression — pluggable context engine with LLM-powered summarization.

Compresses conversation buffers when they exceed a threshold, replacing
old turns with an LLM-generated summary. Preserves frozen snapshots
(MEMORY.md / USER.md).

Architecture:
    ContextEngine (ABC)     ← LLMCompressor
        Defines compress() signature
    Compressor
        Orchestrator: checks buffer size, calls engine, applies result

Usage:
    engine = LLMCompressor(provider=my_provider, max_turns=15)
    compressor = Compressor(engine=engine, max_turns=15)
    summary = compressor.compress(turn_buffer)
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any

logger = logging.getLogger(__name__)

# ─── Defaults ────────────────────────────────────────────────────────────────

DEFAULT_MAX_TURNS: int = 15
"""Default buffer size threshold that triggers compression."""

COMPRESSIBLE_COUNT: int = 4
"""Number of oldest turns to compress when threshold is exceeded."""

_COMPRESSION_PROMPT_TEMPLATE: str = (
    "Сожми эти {n} сообщений из диалога в 2-3 предложения. "
    "Сохрани ключевые факты, принятые решения и важные детали. "
    "Не добавляй ничего от себя — только сжатие того, что сказано:\n\n"
    "{turns}"
)

# ─── Abstract engine ─────────────────────────────────────────────────────────


class ContextEngine(ABC):
    """Pluggable compression engine. Implementations define how compression
    is performed (LLM, rule-based, etc.).
    """

    @abstractmethod
    def compress(self, turns: list[dict[str, Any]]) -> str:
        """Compress a list of conversation turns into a short summary.

        Args:
            turns: List of turn dicts, each with at least ``role`` and ``content``.

        Returns:
            Compressed summary string (2-3 sentences).
        """
        ...


class LLMCompressor(ContextEngine):
    """LLM-powered context compressor.

    Calls the configured provider with a summarization prompt and returns
    the compressed text.

    Attributes:
        provider: An LLM provider with a ``generate(messages, context)`` method.
        temperature: LLM temperature for summarization (low = deterministic).
        max_tokens: Max tokens in the summary.
    """

    def __init__(
        self,
        provider: Any,
        temperature: float = 0.3,
        max_tokens: int = 250,
    ) -> None:
        self._provider = provider
        self._temperature = temperature
        self._max_tokens = max_tokens

    def format_turns(self, turns: list[dict[str, Any]]) -> str:
        """Format turns into a text block for the LLM prompt."""
        lines: list[str] = []
        for t in turns:
            role = t.get("role", "?")
            content = t.get("content", "")
            lines.append(f"[{role}]: {content}")
        return "\n".join(lines)

    def compress(self, turns: list[dict[str, Any]]) -> str:
        """Compress turns via LLM summarization.

        Args:
            turns: Conversation turns to compress.

        Returns:
            Compressed summary string, or empty string on failure.
        """
        if not turns:
            return ""

        formatted = self.format_turns(turns)
        prompt = _COMPRESSION_PROMPT_TEMPLATE.format(
            n=len(turns),
            turns=formatted,
        )

        try:
            result = self._provider.generate(
                messages=[{"role": "user", "content": prompt}],
                context={"temperature": self._temperature, "max_tokens": self._max_tokens},
            )
            return (result or "").strip()
        except Exception as exc:
            logger.warning("LLM compression failed: %s", exc)
            return ""


# ─── Orchestrator ────────────────────────────────────────────────────────────


class Compressor:
    """Orchestrator that decides when and what to compress.

    Checks buffer size against configurable threshold, extracts the oldest
    turns, runs the engine, and returns the summary.

    Does NOT modify the turn_buffer itself — that is the caller's responsibility
    (via MemorySummarizer.apply_compression or similar).

    Attributes:
        engine: The compression engine (e.g. LLMCompressor).
        max_turns: Buffer size threshold. Compression triggers when
            ``len(turn_buffer) > max_turns``.
        compressible_count: How many oldest turns to compress at once.
    """

    def __init__(
        self,
        engine: ContextEngine | None = None,
        max_turns: int = DEFAULT_MAX_TURNS,
        compressible_count: int = COMPRESSIBLE_COUNT,
    ) -> None:
        self.engine = engine
        self.max_turns = max_turns
        self.compressible_count = compressible_count

    def should_compress(self, turn_buffer: list[dict[str, Any]]) -> bool:
        """Check if the buffer exceeds the threshold.

        Args:
            turn_buffer: The current conversation turn buffer.

        Returns:
            True if compression is warranted.
        """
        return len(turn_buffer) > self.max_turns

    def get_old_turns(self, turn_buffer: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Return the oldest compressible turns (or empty list if buffer too small).

        Args:
            turn_buffer: The current conversation turn buffer.

        Returns:
            Oldest N turns, or [] if the buffer is not large enough.
        """
        if len(turn_buffer) <= self.compressible_count:
            return []
        return turn_buffer[: self.compressible_count]

    def compress(
        self,
        turn_buffer: list[dict[str, Any]],
    ) -> str:
        """Run compression: check threshold → extract old turns → compress.

        Args:
            turn_buffer: The current turn buffer.

        Returns:
            Compressed summary string. Empty string if nothing to compress
            or engine is not configured.
        """
        if not self.should_compress(turn_buffer):
            return ""

        old_turns = self.get_old_turns(turn_buffer)
        if not old_turns:
            return ""

        if self.engine is None:
            logger.warning("No compression engine configured")
            return ""

        summary = self.engine.compress(old_turns)
        logger.info(
            "Compressed %d turns into %d chars",
            len(old_turns),
            len(summary),
        )
        return summary


# ─── Convenience ─────────────────────────────────────────────────────────────


def compress_context(
    turn_buffer: list[dict[str, Any]],
    provider: Any | None = None,
    max_turns: int = DEFAULT_MAX_TURNS,
) -> str:
    """One-shot context compression using an LLM provider.

    Args:
        turn_buffer: The conversation turn buffer.
        provider: LLM provider instance. If None, returns empty.
        max_turns: Buffer size threshold.

    Returns:
        Compressed summary, or empty string.
    """
    if provider is None:
        return ""
    engine = LLMCompressor(provider=provider)
    compressor = Compressor(engine=engine, max_turns=max_turns)
    return compressor.compress(turn_buffer)
