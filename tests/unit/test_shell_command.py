"""Unit tests for case-insensitive leading-token shell normalization."""

from __future__ import annotations

import pytest

from antigona.tools.shell_command import normalize_shell_command_first_token


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # Bare capitalized commands fold to lowercase.
        ("Pwd", "pwd"),
        ("Ls -la", "ls -la"),
        ("Apt update", "apt update"),
        ("Apt install uv", "apt install uv"),
        ("Sudo apt update", "sudo apt update"),
        # Already-lowercase leading tokens are untouched (incl. args).
        ("pwd", "pwd"),
        ("ls -la", "ls -la"),
        ("echo Hello World", "echo Hello World"),
        ("apt update", "apt update"),
        # Structural leading tokens must never be folded.
        ("/usr/bin/Ls", "/usr/bin/Ls"),
        ("./Script.sh", "./Script.sh"),
        ("PATH=/usr/bin ls", "PATH=/usr/bin ls"),
        ("$PWD", "$PWD"),
        ('"Apt" install', '"Apt" install'),
        # Leading whitespace is preserved; the token still folds.
        ("  Apt  update", "  apt  update"),
        ("\tApt update", "\tapt update"),
        # Edge cases.
        ("", ""),
        ("   ", "   "),
        ("Apt", "apt"),
    ],
)
def test_normalize_shell_command_first_token(raw: str, expected: str) -> None:
    assert normalize_shell_command_first_token(raw) == expected


def test_arguments_case_is_preserved() -> None:
    # Only the first token may fold; argument case is sacrosanct.
    assert normalize_shell_command_first_token("grep -E 'ApT'") == "grep -E 'ApT'"
    assert normalize_shell_command_first_token("cat /Etc/PassWd") == "cat /Etc/PassWd"
