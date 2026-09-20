from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from antigona.shell import DockerShellTool, ShellInput


class FakeProcess:
    def __init__(self, stdout: bytes, stderr: bytes, returncode: int) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.terminated = False

    def communicate(self, timeout: int) -> tuple[bytes, bytes]:
        del timeout
        return self.stdout, self.stderr

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True


def tool_with_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    stdout: bytes,
    stderr: bytes,
    returncode: int,
    output_cap: int = 65_536,
) -> DockerShellTool:
    process = FakeProcess(stdout, stderr, returncode)
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: process)
    return DockerShellTool(tmp_path / "workspace", output_cap=output_cap)


def test_success_exposes_only_sanitized_stdout_and_never_stderr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stderr_marker = b"SYNTHETIC_SUCCESS_STDERR_MARKER"
    tool = tool_with_process(
        tmp_path,
        monkeypatch,
        stdout=b"safe stdout",
        stderr=b"Traceback: " + stderr_marker,
        returncode=0,
    )

    result = tool.execute(ShellInput(("printf", "safe")))

    assert result.ok is True
    assert result.data == {"output": "safe stdout"}
    assert result.error is None
    assert stderr_marker.decode() not in repr(result)
    assert "Traceback" not in repr(result)


def test_single_string_argv_becomes_a_container_shell_line(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LLM packs 'echo WIP-SHELL-CHECK' into one argv — it is a shell LINE.

    FP-L03c: a packed element is the container shell's command line, so it must
    reach the container as ``/bin/sh -c <line>`` byte-for-byte — never
    ``shlex.split`` into argv (which the sandbox backend then re-quoted into
    literals, killing ``$``, globs and substitutions).
    """
    process = FakeProcess(b"WIP-SHELL-CHECK", b"", 0)
    popen_mock = MagicMock(return_value=process)
    monkeypatch.setattr(subprocess, "Popen", popen_mock)

    tool = DockerShellTool(tmp_path / "workspace", output_cap=65_536)
    result = tool.execute(ShellInput(("echo WIP-SHELL-CHECK",)))

    assert result.ok is True
    assert result.data == {"output": "WIP-SHELL-CHECK"}
    # The docker argv must carry the command line UNSPLIT and UNQUOTED.
    spawned: list[str] = list(popen_mock.call_args.args[0])
    assert spawned[-3:] == ["/bin/sh", "-c", "echo WIP-SHELL-CHECK"]
    assert "'echo WIP-SHELL-CHECK'" not in spawned


def test_nonzero_exit_returns_only_fixed_failure_without_stdout_or_stderr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = b"SYNTHETIC_FAILED_STREAM_MARKER"
    tool = tool_with_process(
        tmp_path,
        monkeypatch,
        stdout=marker,
        stderr=b"Traceback: " + marker,
        returncode=7,
    )

    result = tool.execute(ShellInput(("false",)))

    assert result.ok is False
    assert result.status == "failed"
    assert result.data == {}
    assert result.error.startswith("tool exited non-zero")
    assert marker.decode() not in repr(result)
    assert "Traceback" not in repr(result)


def test_file_not_found_exception_is_fixed_mapped_without_exception_detail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = "SYNTHETIC_FILE_NOT_FOUND_MARKER"

    def missing(*_args: Any, **_kwargs: Any) -> FakeProcess:
        raise FileNotFoundError(marker)

    monkeypatch.setattr(subprocess, "Popen", missing)
    tool = DockerShellTool(tmp_path / "workspace")

    result = tool.execute(ShellInput(("printf", "safe")))

    assert result.ok is False
    assert result.data == {}
    assert result.error == "sandbox unavailable"
    assert marker not in repr(result)


def test_unexpected_launch_exception_is_fixed_mapped_without_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = "SYNTHETIC_UNEXPECTED_LAUNCH_EXCEPTION_MARKER"

    def explode(*_args: Any, **_kwargs: Any) -> FakeProcess:
        raise RuntimeError(marker)

    monkeypatch.setattr(subprocess, "Popen", explode)
    tool = DockerShellTool(tmp_path / "workspace")

    result = tool.execute(ShellInput(("missing-runtime",)))

    assert result.ok is False
    assert result.data == {}
    assert result.error == "tool execution failed"
    assert marker not in repr(result)


def test_stdout_is_redacted_in_full_before_output_cap_crossing_token_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_cap = 48
    provider_token = b"hf_" + b"SYNTHETIC_BOUNDARY_MARKER_123456789"
    stdout = (b"x" * 44) + provider_token
    tool = tool_with_process(
        tmp_path,
        monkeypatch,
        stdout=stdout,
        stderr=b"",
        returncode=0,
        output_cap=output_cap,
    )

    result = tool.execute(ShellInput(("printf", "safe")))

    assert result.ok is True
    output = result.data["output"]
    assert isinstance(output, str)
    assert len(output) <= output_cap
    assert provider_token.decode() not in output
    assert "hf_" not in output
    assert output != stdout[:output_cap].decode()


def test_recover_named_execution_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        cmd_str = " ".join(cmd)
        if "inspect" in cmd_str and "--format" not in cmd_str:
            return subprocess.CompletedProcess(cmd, 0, stdout="{}", stderr="")
        if "wait" in cmd_str:
            return subprocess.CompletedProcess(cmd, 0, stdout="0", stderr="")
        if "--format={{.State.ExitCode}}" in cmd_str:
            return subprocess.CompletedProcess(cmd, 0, stdout="0\n", stderr="")
        if "logs" in cmd_str:
            return subprocess.CompletedProcess(cmd, 0, stdout="recovered stdout\n", stderr="")
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    tool = DockerShellTool(tmp_path / "workspace")
    res = tool._recover_named_execution("antigona-test-id")

    assert res is not None
    assert res.ok is True
    # FP-L23R: the recovered container's stdout keeps its line structure (the
    # container wrote "recovered stdout\n"); this value is what the durable
    # artifact and the verifier's effect facts are built from.
    assert res.data == {"output": "recovered stdout\n"}


def test_recover_named_execution_failure_on_nonzero_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        cmd_str = " ".join(cmd)
        if "inspect" in cmd_str and "--format" not in cmd_str:
            return subprocess.CompletedProcess(cmd, 0, stdout="{}", stderr="")
        if "wait" in cmd_str:
            return subprocess.CompletedProcess(cmd, 0, stdout="1", stderr="")
        if "--format={{.State.ExitCode}}" in cmd_str:
            return subprocess.CompletedProcess(cmd, 0, stdout="1\n", stderr="")
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    tool = DockerShellTool(tmp_path / "workspace")
    res = tool._recover_named_execution("antigona-test-id")

    assert res is not None
    assert res.ok is False
    assert "exited with code 1" in (res.error or "")


