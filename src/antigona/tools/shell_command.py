"""Case-insensitive leading-token normalization for shell commands.

Users (especially from a phone/Telegram) commonly type ``Apt``, ``Pwd``,
``Ls`` with a capital letter. Any real shell (/bin/sh, bash) is
case-sensitive, so those fail with ``not found`` even though the user meant
``apt``/``pwd``/``ls``. This module folds ONLY the leading command token to
lowercase so casual capitalization resolves to the actual (all-lowercase)
system binaries.

It is deliberately conservative: arguments and the rest of the line are left
byte-for-byte intact, and a leading token that looks structural — a path
(contains ``/``), a ``var=value`` assignment (contains ``=``), a ``$var``
expansion, or a quoted word — is returned unchanged. Command names that are
genuinely case-sensitive survive untouched.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

_LEADING_TOKEN_RE = re.compile(r"^(\S+)(\s.*)?$", re.DOTALL)
_TOOL_PREFIX_RE = re.compile(r"^(?:shell|bash|sh|execute):\s*", re.IGNORECASE)


def strip_shell_tool_prefix(command: str) -> str:
    """Strip a leading literal tool prefix ('shell:', 'bash:', 'sh:', 'execute:').

    >>> strip_shell_tool_prefix("shell: uname -a")
    'uname -a'
    >>> strip_shell_tool_prefix("bash: ls -la")
    'ls -la'
    >>> strip_shell_tool_prefix("sh: echo hello")
    'echo hello'
    >>> strip_shell_tool_prefix("execute: pwd")
    'pwd'
    >>> strip_shell_tool_prefix("shell:echo C5_APPROVED_SHELL_OK && uname -s")
    'echo C5_APPROVED_SHELL_OK && uname -s'
    """
    if not command:
        return command
    stripped = command.lstrip()
    indent = command[: len(command) - len(stripped)]
    match = _TOOL_PREFIX_RE.match(stripped)
    if not match:
        return command
    return indent + stripped[match.end():]


def strip_shell_tool_prefix_argv(command: Sequence[str]) -> tuple[str, ...]:
    """Strip leading literal tool prefix from argv (single string or token sequence)."""
    if not command:
        return tuple(command)
    first = str(command[0]).strip()
    if re.fullmatch(r"(?i)^(?:shell|bash|sh|execute):$", first):
        return tuple(str(x) for x in command[1:])
    if _TOOL_PREFIX_RE.match(first):
        cleaned = strip_shell_tool_prefix(first)
        rest = tuple(str(x) for x in command[1:])
        return ((cleaned,) + rest) if cleaned else rest
    return tuple(str(x) for x in command)


def normalize_shell_command_first_token(command: str) -> str:
    """Return *command* with its leading token lowercased when it is a bare
    command word; otherwise return *command* unchanged.

    >>> normalize_shell_command_first_token("Apt install uv")
    'apt install uv'
    >>> normalize_shell_command_first_token("Pwd")
    'pwd'
    >>> normalize_shell_command_first_token("echo Hello")
    'echo Hello'
    >>> normalize_shell_command_first_token("PATH=/x ls")
    'PATH=/x ls'
    """
    if not command:
        return command
    stripped = command.lstrip()
    indent = command[: len(command) - len(stripped)]
    match = _LEADING_TOKEN_RE.match(stripped)
    if not match:
        return command
    token, rest = match.group(1), match.group(2) or ""
    # Leave path/assignment/expansion/quoted leading tokens alone.
    if any(ch in token for ch in ("/", "=", "$", '"', "'")):
        return command
    lower = token.lower()
    if lower == token:
        return command
    return indent + lower + rest


_SYSTEM_PACKAGES = frozenset({"git", "curl", "wget", "make", "gcc", "bash"})
_PIP_INSTALL_RE = re.compile(r"\Apip(?:3)? install (git|curl|wget|make|gcc|bash)\Z")


def normalize_system_package_install(command: tuple[str, ...], *, alpine: bool) -> tuple[str, ...]:
    """Map only the exact safe pip-install form to the image OS package manager.

    The planner normally supplies structured argv.  The legacy ``sh -c`` shape
    is accepted only when its payload is exactly ``pip install PACKAGE`` (no
    quoting, operators, substitutions, redirects, chaining, or extra args).
    Everything else is returned byte-for-byte unchanged.
    """
    package: str | None = None
    if len(command) == 3 and command[0] in {"pip", "pip3"} and command[1] == "install":
        candidate = command[2]
        if candidate in _SYSTEM_PACKAGES:
            package = candidate
    elif len(command) == 3 and command[:2] == ("sh", "-c"):
        match = _PIP_INSTALL_RE.fullmatch(command[2])
        if match:
            package = match.group(1)
    if package is None:
        return command
    return ("apk", "add", package) if alpine else ("apt-get", "-y", "install", package)
