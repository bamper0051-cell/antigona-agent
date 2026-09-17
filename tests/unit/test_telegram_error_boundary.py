"""Regression policy for secret-safe Telegram user-facing failures."""

from __future__ import annotations

import ast
import inspect

import antigona.channels.telegram.bot as telegram_bot


def _caught_exception_fstring_lines() -> list[int]:
    source = inspect.getsource(telegram_bot)
    tree = ast.parse(source)
    findings: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler) or not isinstance(node.name, str):
            continue
        for descendant in ast.walk(node):
            if not isinstance(descendant, ast.JoinedStr):
                continue
            if any(
                isinstance(part, ast.FormattedValue)
                and isinstance(part.value, ast.Name)
                and part.value.id == node.name
                for part in descendant.values
            ):
                findings.append(descendant.lineno)
    return sorted(findings)


def test_caught_exceptions_are_never_interpolated_into_telegram_text() -> None:
    assert _caught_exception_fstring_lines() == []


def test_known_untrusted_failure_payloads_are_not_forwarded() -> None:
    source = inspect.getsource(telegram_bot)
    forbidden_fragments = (
        'f"❌ {result.error}"',
        "❌ {result.error}",
        'f"❌ Archive failed: {result.error}"',
        'f"❌ Archive error: {exc}"',
        'f"{result.stderr.strip() or result.stdout.strip()}"',
        "'; '.join(fail_msgs)",
    )
    assert [fragment for fragment in forbidden_fragments if fragment in source] == []
