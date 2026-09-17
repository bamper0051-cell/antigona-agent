"""Tests for compound shell command handling in DockerShellTool.

RED→GREEN: compound commands with &&, >, |, ; must be wrapped in
/bin/sh -c rather than shlex.split into argv tokens.
"""
from __future__ import annotations

import re
import shlex

import pytest


def _has_shell_operators(cmd: str) -> bool:
    """Mirror the detection logic in shell.py."""
    return bool(re.search(r"&&|\|\||\||;|>>?(?!=)", cmd))


@pytest.mark.parametrize(
    "cmd,expected",
    [
        ("echo hello", False),
        ("mkdir -p /tmp/foo && echo done", True),
        ("printf 'x' > /tmp/out.txt", True),
        ("cat a.txt | grep foo", True),
        ("echo one; echo two", True),
        ("echo 'no operators here'", False),
        ("ls -la >> log.txt", True),
        ("cmd1 || cmd2", True),
    ],
)
def test_shell_operator_detection(cmd: str, expected: bool) -> None:
    assert _has_shell_operators(cmd) == expected


def test_compound_command_not_shlex_split() -> None:
    """shlex.split on a compound command turns && and > into argv tokens —
    this was the T30 root cause."""
    compound = "mkdir -p test_dir && printf 'ONE\\n' > test_dir/one.txt"
    tokens = shlex.split(compound)
    assert "&&" in tokens, "shlex.split keeps && as a token (the bug)"
    assert ">" in tokens, "shlex.split keeps > as a token (the bug)"


def test_compound_command_wrapped_in_sh_c() -> None:
    """DockerShellTool.execute must normalize compound commands to /bin/sh -c
    before passing to build_run_argv. We verify by intercepting Popen."""
    import tempfile
    import unittest.mock as mock
    from pathlib import Path

    from antigona.shell import DockerShellTool, ShellInput

    with tempfile.TemporaryDirectory() as td:
        tool = DockerShellTool(workspace=Path(td))

        compound = "mkdir -p d && printf 'X' > d/x.txt && cat d/x.txt"
        inp = ShellInput(command=(compound,), execution_id="test-compound")

        captured_argv: list[list[str]] = []

        fake_proc = mock.MagicMock()
        fake_proc.communicate.return_value = (b"X", b"")
        fake_proc.returncode = 0
        fake_proc.pid = 12345

        def fake_popen(argv, **kw):
            captured_argv.append(list(argv))
            return fake_proc

        # Also mock _recover to skip docker inspect
        with mock.patch("subprocess.Popen", side_effect=fake_popen), \
             mock.patch.object(tool, "_recover_named_execution", return_value=None):
            tool.execute(inp)

        # First Popen call is the docker run
        assert captured_argv, "No Popen calls captured"
        docker_run_argv = captured_argv[0]

        # Must end with: image /bin/sh -c <compound>
        assert "/bin/sh" in docker_run_argv, (
            f"Expected /bin/sh in docker argv: {docker_run_argv}"
        )
        sh_idx = docker_run_argv.index("/bin/sh")
        assert docker_run_argv[sh_idx + 1] == "-c"
        assert docker_run_argv[sh_idx + 2] == compound

        # && must NOT be a standalone token
        standalone = [t for t in docker_run_argv if t in ("&&", ">", "|", ";")]
        assert not standalone, f"Operators as standalone tokens: {standalone}"


def test_simple_command_still_shlex_split() -> None:
    """Simple commands (no shell operators) should still be shlex.split."""
    import tempfile
    import unittest.mock as mock
    from pathlib import Path

    from antigona.shell import DockerShellTool, ShellInput

    with tempfile.TemporaryDirectory() as td:
        tool = DockerShellTool(workspace=Path(td))
        inp = ShellInput(command=("echo hello world",), execution_id="test-simple")

        captured_argv: list[list[str]] = []

        fake_proc = mock.MagicMock()
        fake_proc.communicate.return_value = (b"hello world", b"")
        fake_proc.returncode = 0
        fake_proc.pid = 12345

        def fake_popen(argv, **kw):
            captured_argv.append(list(argv))
            return fake_proc

        with mock.patch("subprocess.Popen", side_effect=fake_popen), \
             mock.patch.object(tool, "_recover_named_execution", return_value=None):
            tool.execute(inp)

        assert captured_argv
        docker_run_argv = captured_argv[0]
        # Simple: shlex split, so "echo", "hello", "world" as separate tokens
        assert "echo" in docker_run_argv
        assert "hello" in docker_run_argv
        assert "world" in docker_run_argv
        assert "/bin/sh" not in docker_run_argv
