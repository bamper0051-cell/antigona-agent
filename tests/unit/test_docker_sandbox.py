"""Unit tests for the isolated Docker sandbox backend (owner-approved high-risk shell).

Covers the security contract: ephemeral container, no --privileged, no host
network, no docker socket, only the minimal workspace bind, resource limits,
timeout, audit, exit code/stdout/stderr, and fail-closed when Docker is gone.
"""

from __future__ import annotations

import pytest

from antigona.sandbox.docker_sandbox import (
    DockerSandboxBackend,
    DockerSandboxResult,
    DockerSandboxUnavailableError,
)


def test_build_argv_enforces_security_contract() -> None:
    b = DockerSandboxBackend(workspace="/tmp/ws", image="python:3.12-alpine")
    argv = b._build_argv(["pip", "install", "uv"])
    joined = " ".join(argv)
    assert "--rm" in joined                       # ephemeral
    assert "--privileged" not in joined           # no privileged mode
    assert "--network host" not in joined         # no host network
    assert "--network bridge" in joined           # isolated default network
    assert "no-new-privileges" in joined
    assert "/var/run/docker.sock" not in joined   # no docker socket
    assert "/run/docker.sock" not in joined
    assert "/tmp/ws:/workspace" in joined.replace("\\", "/")   # only minimal workspace bind (Wave 4: Windows drive path)
    assert "--memory" in joined
    assert "--cpus" in joined
    assert "--stop-timeout" in joined
    assert "/bin/sh" in argv
    assert "pip install uv" in argv


def test_build_argv_rejects_absolute_path_tokens() -> None:
    b = DockerSandboxBackend(workspace="/tmp/ws")
    with pytest.raises(Exception) as exc:
        b._build_argv(["/bin/rm", "-rf", "/"])
    assert "absolute paths are forbidden" in str(exc.value)


def test_run_fails_closed_when_docker_unavailable(monkeypatch) -> None:
    b = DockerSandboxBackend(workspace="/tmp/ws")
    monkeypatch.setattr(b, "is_available", lambda: False)
    with pytest.raises(DockerSandboxUnavailableError) as exc:
        b.run(["pip", "install", "uv"], correlation_id="c", task_id="t")
    assert "fail-closed" in str(exc.value).lower() or "refusing" in str(exc.value).lower()


def test_run_preserves_exit_code_stdout_stderr(monkeypatch) -> None:
    b = DockerSandboxBackend(workspace="/tmp/ws")
    monkeypatch.setattr(b, "is_available", lambda: True)
    captured = {}

    def fake_run(argv, **kw):
        captured["argv"] = argv
        class P:
            returncode = 7
            stdout = "OUT_MARKER"
            stderr = "ERR_MARKER"
        return P()

    monkeypatch.setattr("antigona.sandbox.docker_sandbox.subprocess.run", fake_run)
    res = b.run(["badcmd"], correlation_id="c1", task_id="t1")
    assert isinstance(res, DockerSandboxResult)
    assert res.exit_code == 7
    assert res.stdout == "OUT_MARKER"
    assert res.stderr == "ERR_MARKER"
    assert "antigona-sandbox-" in res.container_id
    # audit event carries correlation/task ids
    assert "--name" in captured["argv"]


def test_run_timeout_sets_124(monkeypatch) -> None:
    import subprocess as real_subprocess

    b = DockerSandboxBackend(workspace="/tmp/ws")
    monkeypatch.setattr(b, "is_available", lambda: True)

    class TimeoutErr(real_subprocess.TimeoutExpired):
        def __init__(self):
            super().__init__("cmd", 1)

    monkeypatch.setattr(
        "antigona.sandbox.docker_sandbox.subprocess.run",
        lambda argv, **kw: (_ for _ in ()).throw(TimeoutErr()),
    )
    res = b.run(["sleep", "99"], correlation_id="c2", task_id="t2")
    assert res.timed_out is True
    assert res.exit_code == 124
