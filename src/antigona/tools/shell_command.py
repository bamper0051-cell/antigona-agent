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

#: Canonical shell wrapper for a packed command LINE (FP-L03c).
SHELL_ARGV_PREFIX = ("/bin/sh", "-c")


def to_shell_argv(command: Sequence[str]) -> tuple[str, ...]:
    """Return the argv the container must execute for *command*.

    Contract (FP-L03c): a SINGLE packed element is the container shell's
    command LINE, not an argv, so it is always wrapped as
    ``("/bin/sh", "-c", <line>)`` with the line kept byte-for-byte.  That is
    what preserves ``$VAR``, ``$((...))``, globs, substitutions, redirections
    and ``sh`` aliases inside the container.  ``shlex.split`` used to run here
    and turned ``echo $((2+2))`` into the argv ``["echo", "$((2+2))"]``, which
    ``docker_sandbox`` then re-quoted into a literal; a lone token (``ll``)
    became ``argv[0]`` and docker reported ``error finding executable`` (127).

    A REAL argv (two or more elements) is returned untouched: joining it into a
    shell string would destroy the caller's argument boundaries, and only an
    explicitly packed line is a shell command line.

    >>> to_shell_argv(("echo $((2+2))",))
    ('/bin/sh', '-c', 'echo $((2+2))')
    >>> to_shell_argv(("ll",))
    ('/bin/sh', '-c', 'll')
    >>> to_shell_argv(("echo", "$((2+2))"))
    ('echo', '$((2+2))')
    >>> to_shell_argv(("/bin/sh", "-c", "pwd && echo ok"))
    ('/bin/sh', '-c', 'pwd && echo ok')
    """
    cmd = tuple(str(x) for x in command)
    if len(cmd) != 1:
        return cmd
    line = cmd[0].strip()
    if not line:
        return cmd
    return (*SHELL_ARGV_PREFIX, line)


#: apt drops privileges to its "``_apt``" sandbox user; under ``--cap-drop=ALL``
#: (no CAP_SETGID/SETUID for arbitrary targets) that drop fails and the partial
#: file chown/chmod then corrupts the package index ("Unable to locate
#: package").  Disabling the drop lets apt run as the container user (root, with
#: DAC_OVERRIDE), which can write /var/lib/apt — safe in a throwaway,
#: non-privileged container.
APT_SANDBOX_OPTION = ("-o", "APT::Sandbox::User=root")
_APT_LEADING_RE = re.compile(r"(apt|apt-get)(?=\s|$)")


def normalize_apt_sandbox_user(command: Sequence[str]) -> tuple[str, ...]:
    """Insert the apt sandbox workaround for *apt*/*apt-get* commands only.

    Accepts both a real argv (``("apt", "install", "uv")``) and the FP-L03c
    packed shell line (``("/bin/sh", "-c", "apt install uv")``).  The leading
    command word must be exactly ``apt`` or ``apt-get``; every other command —
    and any other position (``echo apt install x``) — is returned unchanged, so
    the payload keeps its bytes and the shell line is not re-quoted.

    >>> normalize_apt_sandbox_user(("apt", "install", "uv"))
    ('apt', '-o', 'APT::Sandbox::User=root', 'install', 'uv')
    >>> normalize_apt_sandbox_user(("/bin/sh", "-c", "apt-get update"))
    ('/bin/sh', '-c', 'apt-get -o APT::Sandbox::User=root update')
    >>> normalize_apt_sandbox_user(("echo", "apt install x"))
    ('echo', 'apt install x')
    """
    cmd = tuple(str(x) for x in command)
    if len(cmd) == 3 and cmd[:2] == SHELL_ARGV_PREFIX:
        line = cmd[2].lstrip()
        match = _APT_LEADING_RE.match(line)
        if match is None:
            return cmd
        indent = cmd[2][: len(cmd[2]) - len(line)]
        rewritten = f"{indent}{match.group(1)} {' '.join(APT_SANDBOX_OPTION)}{line[match.end():]}"
        return (*SHELL_ARGV_PREFIX, rewritten)
    if cmd and cmd[0] in {"apt", "apt-get"}:
        return (cmd[0], *APT_SANDBOX_OPTION, *cmd[1:])
    return cmd


def normalize_system_package_install(command: tuple[str, ...], *, alpine: bool) -> tuple[str, ...]:
    """Map only the exact safe pip-install form to the image OS package manager.

    The planner normally supplies structured argv.  The legacy ``sh -c`` shape
    (and the FP-L03c ``/bin/sh -c`` wrapper) is accepted only when its payload is
    exactly ``pip install PACKAGE`` (no quoting, operators, substitutions,
    redirects, chaining, or extra args).  Everything else is returned
    byte-for-byte unchanged.
    """
    package: str | None = None
    if len(command) == 3 and command[0] in {"pip", "pip3"} and command[1] == "install":
        candidate = command[2]
        if candidate in _SYSTEM_PACKAGES:
            package = candidate
    elif len(command) == 3 and command[:2] in {("sh", "-c"), SHELL_ARGV_PREFIX}:
        match = _PIP_INSTALL_RE.fullmatch(command[2])
        if match:
            package = match.group(1)
    if package is None:
        return command
    return ("apk", "add", package) if alpine else ("apt-get", "-y", "install", package)
