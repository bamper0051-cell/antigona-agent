from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from antigona.sandbox.microvm import MicroVMProfile, MicroVMRunner, MicroVMUnavailableError
from antigona.worker.tools.common import WorkspaceGuard
from antigona.worker.tools.shell_tool import WorkspaceShellTool


def test_high_risk_command_routes_to_microvm_mock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os.path, "exists", lambda p: True)
    monkeypatch.setattr(os, "access", lambda p, m: True)

    calls: list[list[str]] = []

    def mock_runner(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="output from microvm", stderr="")

    guard = WorkspaceGuard(tmp_path)
    profile = MicroVMProfile(workspace=tmp_path)
    runner = MicroVMRunner.create(profile, backend="firecracker", runner=mock_runner)

    shell = WorkspaceShellTool(guard, sandbox_runtime="firecracker", microvm=runner)
    res = shell.run(["python", "-c", "print('hello')"])

    assert res.exit_code == 0
    assert res.stdout == "output from microvm"
    assert res.untrusted is True
    assert len(calls) == 2  # spawn (firecracker) + exec (fc-exec)


def test_network_goes_through_egress(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os.path, "exists", lambda p: True)
    monkeypatch.setattr(os, "access", lambda p, m: True)

    captured_inputs: list[str] = []

    def mock_runner(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if "input" in kwargs and kwargs["input"]:
            captured_inputs.append(str(kwargs["input"]))
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    guard = WorkspaceGuard(tmp_path)
    egress_url = "http://127.0.0.1:8080"
    profile = MicroVMProfile(workspace=tmp_path, egress_endpoint=egress_url)
    runner = MicroVMRunner.create(profile, backend="firecracker", runner=mock_runner)

    shell = WorkspaceShellTool(guard, sandbox_runtime="firecracker", microvm=runner)
    res = shell.run(["curl", "http://api.example.com"])

    assert res.exit_code == 0
    assert len(captured_inputs) > 0
    assert egress_url in captured_inputs[0]


def test_runtime_unavailable_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os.path, "exists", lambda p: False)

    guard = WorkspaceGuard(tmp_path)
    shell = WorkspaceShellTool(guard, sandbox_runtime="firecracker", microvm=None)

    with pytest.raises(MicroVMUnavailableError, match="unavailable"):
        shell.run(["python", "-c", "import os"])


def test_low_risk_command_stays_host(tmp_path: Path) -> None:
    guard = WorkspaceGuard(tmp_path)
    shell = WorkspaceShellTool(guard, sandbox_runtime="firecracker", microvm=None)

    res = shell.run(["echo", "low-risk"])

    assert res.exit_code == 0
    assert "low-risk" in res.stdout
    assert res.untrusted is False


def test_real_spawn_skipped_without_env(tmp_path: Path) -> None:
    if os.getenv("ANTIGONA_MICROVM_REAL") != "1":
        pytest.skip("Real micro-VM spawn skipped without ANTIGONA_MICROVM_REAL=1")

    profile = MicroVMProfile(workspace=tmp_path)
    runner = MicroVMRunner.create(profile, backend="firecracker")

    runner.spawn()
    try:
        res = runner.exec(["echo", "real-vm"])
        assert res.exit_code == 0
    finally:
        runner.teardown()

