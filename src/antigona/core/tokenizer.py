"""DeepSeek token counting utility.

Thin, safe wrapper around ``deepseek-tokenizer`` (AndersonBY). Provides exact
token accounting for DeepSeek contexts with a graceful fallback when the
optional package is not installed (so nothing hard-fails at import time).

Usage::

    from antigona.core.tokenizer import count_tokens, tokenizer_available

    n = count_tokens("привет мир")
    if tokenizer_available():
        # exact DeepSeek BPE count
        ...
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

__all__ = ["count_tokens", "tokenizer_available", "get_tokenizer", "truncate_to_tokens"]

try:  # optional dependency — must never break import
    from deepseek_tokenizer import BASE_FOLDER as _BASE_FOLDER
    from deepseek_tokenizer import DeepSeekTokenizer

    _tokenizer: DeepSeekTokenizer | None = None

    def _load() -> Any:
        global _tokenizer
        if _tokenizer is None:
            _tokenizer = DeepSeekTokenizer.from_pretrained(_BASE_FOLDER)
        return _tokenizer

    def tokenizer_available() -> bool:
        return True

except Exception as _exc:  # pragma: no cover — import-path dependent
    _tokenizer = None
    _exc_msg = str(_exc)

    def tokenizer_available() -> bool:
        return False


@lru_cache(maxsize=1)
def _default_tokenizer() -> Any | None:
    if not tokenizer_available():
        return None
    return _load()


def count_tokens(text: str) -> int:
    """Return an exact DeepSeek BPE token count.

    Falls back to a cheap whitespace-based estimate when the optional
    ``deepseek-tokenizer`` package is absent, so callers never crash.
    """
    if not text:
        return 0
    tok = _default_tokenizer()
    if tok is not None:
        try:
            return len(tok.encode(text))
        except Exception:
            pass
    # cheap fallback: ~1.3 tokens per word (DeepSeek-ish heuristic)
    return max(1, len(text.split()) * 13 // 10)


def get_tokenizer() -> Any | None:
    """Return the live DeepSeek tokenizer or None (graceful)."""
    return _default_tokenizer()


def truncate_to_tokens(text: str, max_tokens: int) -> str:
    """Truncate ``text`` to at most ``max_tokens`` tokens (best effort)."""
    if not text or max_tokens <= 0:
        return ""
    tok = _default_tokenizer()
    if tok is None:
        words = text.split()
        keep = max(1, max_tokens * 10 // 13)
        return " ".join(words[:keep])
    try:
        ids = tok.encode(text)
        if len(ids) <= max_tokens:
            return text
        return str(tok.decode(ids[:max_tokens]))
    except Exception:
        return text[: max_tokens * 4]
