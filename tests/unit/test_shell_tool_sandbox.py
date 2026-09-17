"""Unit tests: WorkspaceShellTool high-risk routing to Docker sandbox.

Low-risk allowlisted commands stay on the host. High-risk commands NEVER run on
the host: without owner approval they are refused; with approval they route to
the isolated Docker sandbox. If the sandbox runtime is missing it fails closed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from antigona.sandbox.docker_sandbox import DockerSandboxBackend, DockerSandboxResult
from antigona.worker.tools.common import ToolError, WorkspaceGuard
from antigona.worker.tools.shell_tool import WorkspaceShellTool


def _tool(tmp_path: Path) -> WorkspaceShellTool:
    return WorkspaceShellTool(WorkspaceGuard(tmp_path / "workspace"))


def test_low_risk_command_runs_on_host(tmp_path) -> None:
    t = _tool(tmp_path)
    res = t.run(["echo", "hello"])
    assert res.exit_code == 0
    assert res.stdout.strip() == "hello"
    assert res.untrusted is False


def test_high_risk_without_approval_is_refused(tmp_path) -> None:
    t = _tool(tmp_path)
    with pytest.raises(ToolError) as exc:
        t.run(["pip", "install", "uv"])
    assert "requires owner approval" in str(exc.value)
    assert "not in P0 allowlist" in str(exc.value)


def test_high_risk_with_approval_routes_to_docker_sandbox(tmp_path, monkeypatch) -> None:
    t = _tool(tmp_path)
    fake_backend = DockerSandboxBackend(workspace=str(tmp_path))
    calls = {}

    def fake_run(command, correlation_id="", task_id=""):
        calls["command"] = command
        calls["correlation_id"] = correlation_id
        calls["task_id"] = task_id
        return DockerSandboxResult(
            command=tuple(command), exit_code=0, stdout="SANDBOX_OUT", stderr="", container_id="c-1"
        )

    monkeypatch.setattr(fake_backend, "run", fake_run)
    monkeypatch.setattr(t, "_docker_backend", fake_backend)
    monkeypatch.setattr(fake_backend, "is_available", lambda: True)

    res = t.run(["pip", "install", "uv"], approved=True, correlation_id="corrX", task_id="taskY")
    assert res.stdout == "SANDBOX_OUT"
    assert res.untrusted is True
    assert calls["correlation_id"] == "corrX"
    assert calls["task_id"] == "taskY"


def test_high_risk_approved_but_sandbox_unavailable_fails_closed(tmp_path, monkeypatch) -> None:
    t = _tool(tmp_path)
    fake_backend = DockerSandboxBackend(workspace=str(tmp_path))
    monkeypatch.setattr(fake_backend, "is_available", lambda: False)
    monkeypatch.setattr(t, "_docker_backend", fake_backend)
    from antigona.sandbox.docker_sandbox import DockerSandboxUnavailableError
    with pytest.raises(DockerSandboxUnavailableError):
        t.run(["pip", "install", "uv"], approved=True)


def test_high_risk_non_docker_runtime_keeps_p0_rejection(tmp_path) -> None:
    t = WorkspaceShellTool(
        WorkspaceGuard(tmp_path / "workspace"), sandbox_runtime="unknown"
    )
    with pytest.raises(ToolError) as exc:
        t.run(["pip", "install", "uv"], approved=True)
    assert "not in the P0 allowlist" in str(exc.value)
