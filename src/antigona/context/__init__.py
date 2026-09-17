"""Context builder — persona, policy, history, token budget assembly for LLM calls.

Provides ContextBuilder which assembles a complete message list from
system prompt components (persona, policy rules, session summary) and
conversation history (turn_buffer from MemorySummarizer), applying
a configurable token budget with oldest-message eviction.
"""

from __future__ import annotations

from antigona.context.builder import ContextBuilder, estimate_tokens, truncate_history_by_budget
from antigona.context.compressor import (
    COMPRESSIBLE_COUNT,
    DEFAULT_MAX_TURNS,
    Compressor,
    ContextEngine,
    LLMCompressor,
    compress_context,
)

__all__ = [
    "COMPRESSIBLE_COUNT",
    "Compressor",
    "ContextBuilder",
    "ContextEngine",
    "DEFAULT_MAX_TURNS",
    "LLMCompressor",
    "compress_context",
    "estimate_tokens",
    "truncate_history_by_budget",
]
