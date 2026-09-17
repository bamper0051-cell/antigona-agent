"""Tests for antigona.core.tokenizer."""

from __future__ import annotations

from antigona.core.tokenizer import (
    count_tokens,
    tokenizer_available,
    truncate_to_tokens,
)


def test_count_tokens_empty():
    assert count_tokens("") == 0


def test_count_tokens_positive():
    n = count_tokens("привет мир test hello 123")
    assert n >= 1


def test_count_tokens_available_consistent():
    if tokenizer_available():
        # exact tokenizer gives deterministic positive count
        a = count_tokens("привет мир")
        b = count_tokens("привет мир")
        assert a == b and a >= 1


def test_truncate_empty():
    assert truncate_to_tokens("", 10) == ""


def test_truncate_short_is_identity():
    if tokenizer_available():
        text = "короткая строка"
        assert truncate_to_tokens(text, 10_000) == text


def test_truncate_long_reduces():
    if tokenizer_available():
        long_text = "слово " * 500
        n_full = count_tokens(long_text)
        n_cut = count_tokens(truncate_to_tokens(long_text, 50))
        assert n_cut < n_full
