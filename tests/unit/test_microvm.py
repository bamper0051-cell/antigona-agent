from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from antigona.sandbox.microvm import (
    MicroVMExecResult,
    MicroVMProfile,
    MicroVMRunner,
    MicroVMUnavailableError,
    microvm_available,
)


def test_microvm_available_true_when_kvm_and_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os.path, "exists", lambda p: True if p == "/dev/kvm" else False)
    monkeypatch.setattr(os, "access", lambda p, m: True if p == "/dev/kvm" else False)

    def mock_runner(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args[0], 0, stdout="1.0", stderr="")

    assert microvm_available("firecracker", runner=mock_runner) is True


def test_microvm_available_false_without_kvm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os.path, "exists", lambda p: False if p == "/dev/kvm" else True)

    def mock_runner(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args[0], 0, stdout="1.0", stderr="")

    assert microvm_available("firecracker", runner=mock_runner) is False


def test_microvm_available_false_without_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os.path, "exists", lambda p: True if p == "/dev/kvm" else False)
    monkeypatch.setattr(os, "access", lambda p, m: True if p == "/dev/kvm" else False)

    def failing_runner(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args[0], 1, stdout="", stderr="not found")

    assert microvm_available("firecracker", runner=failing_runner) is False


def test_spawn_exec_teardown_firecracker_mock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os.path, "exists", lambda p: True)
    monkeypatch.setattr(os, "access", lambda p, m: True)

    calls: list[list[str]] = []

    def mock_runner(cmd: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, stdout="hello from vm", stderr="")

    profile = MicroVMProfile(workspace=tmp_path, vcpus=2, mem_mib=512)
    runner = MicroVMRunner.create(profile, backend="firecracker", runner=mock_runner)

    runner.spawn()
    res = runner.exec(["echo", "hello"])

    assert res.exit_code == 0
    assert res.stdout == "hello from vm"
    assert res.untrusted is True
    assert len(calls) == 2

    runner.teardown()
    assert runner._spawned is False


def test_teardown_idempotent_in_finally(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os.path, "exists", lambda p: True)
    monkeypatch.setattr(os, "access", lambda p, m: True)

    def mock_runner(cmd: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    profile = MicroVMProfile(workspace=tmp_path)
    runner = MicroVMRunner.create(profile, backend="firecracker", runner=mock_runner)

    runner.spawn()
    with pytest.raises(ValueError, match="simulated error"):
        try:
            raise ValueError("simulated error during execution")
        finally:
            runner.teardown()

    assert runner._spawned is False
    # Second teardown call should not raise
    runner.teardown()


def test_unavailable_raises_microvm_unavailable_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os.path, "exists", lambda p: False)

    def mock_runner(cmd: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")

    profile = MicroVMProfile(workspace=tmp_path)
    runner = MicroVMRunner.create(profile, backend="firecracker", runner=mock_runner)

    with pytest.raises(MicroVMUnavailableError, match="unavailable on host"):
        runner.spawn()


def test_no_docker_sock_ever_in_argv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os.path, "exists", lambda p: True)
    monkeypatch.setattr(os, "access", lambda p, m: True)

    captured_cmds: list[list[str]] = []
    captured_inputs: list[str] = []

    def mock_runner(cmd: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured_cmds.append(list(cmd))
        if "input" in kwargs and kwargs["input"]:
            captured_inputs.append(str(kwargs["input"]))
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    profile = MicroVMProfile(workspace=tmp_path)
    runner = MicroVMRunner.create(profile, backend="firecracker", runner=mock_runner)
    runner.spawn()

    all_str = " ".join(" ".join(c) for c in captured_cmds) + " ".join(captured_inputs)
    assert "docker.sock" not in all_str


def test_exec_result_marked_untrusted() -> None:
    res = MicroVMExecResult(command=("ls",), exit_code=0, stdout="a\nb", stderr="", untrusted=True)
    assert res.untrusted is True


def test_e2b_backend_lazy_import(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTIGONA_E2B_API_KEY", raising=False)
    monkeypatch.delenv("E2B_API_KEY", raising=False)

    profile = MicroVMProfile(workspace=tmp_path)
    runner = MicroVMRunner.create(profile, backend="e2b")

    # Without API key, should be False
    assert runner.is_available() is False


def test_exec_without_spawn_raises_error(tmp_path: Path) -> None:
    profile = MicroVMProfile(workspace=tmp_path)
    runner = MicroVMRunner.create(profile, backend="firecracker")
    with pytest.raises(MicroVMUnavailableError, match="not spawned"):
        runner.exec(["ls"])


def test_firecracker_spawn_mock_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os.path, "exists", lambda p: True)
    monkeypatch.setattr(os, "access", lambda p, m: True)

    def failing_runner(cmd: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="failed to boot")

    profile = MicroVMProfile(workspace=tmp_path)
    runner = MicroVMRunner.create(profile, backend="firecracker", runner=failing_runner)
    with pytest.raises(MicroVMUnavailableError, match="Firecracker spawn mock failed"):
        runner.spawn()


def test_e2b_spawn_and_exec_mock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTIGONA_E2B_API_KEY", "dummy_key")
    monkeypatch.setattr("antigona.sandbox.microvm.microvm_available", lambda *a, **kw: True)

    calls: list[list[str]] = []

    def mock_runner(cmd: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, stdout="e2b output", stderr="")

    profile = MicroVMProfile(workspace=tmp_path, e2b_api_key="dummy_key")
    runner = MicroVMRunner.create(profile, backend="e2b", runner=mock_runner)

    runner.spawn()
    assert runner._spawned is True

    res = runner.exec(["python", "-c", "print(1)"])
    assert res.stdout == "e2b output"
    assert res.untrusted is True

    runner.teardown()
    assert runner._spawned is False


def test_microvm_available_unknown_backend() -> None:
    assert microvm_available("unknown_backend") is False


def test_teardown_with_process_and_e2b_mock(tmp_path: Path) -> None:
    class DummyProc:
        def terminate(self) -> None:
            raise OSError("simulated proc error")

        def wait(self, timeout: int = 2) -> None:
            pass

    class DummyE2B:
        def kill(self) -> None:
            raise RuntimeError("simulated e2b kill error")

    profile = MicroVMProfile(workspace=tmp_path)
    runner = MicroVMRunner.create(profile, backend="firecracker")
    runner._process = DummyProc()  # type: ignore[assignment]
    runner._e2b_sandbox = DummyE2B()
    runner._spawned = True

    runner.teardown()
    assert runner._spawned is False
    assert runner._process is None
    assert runner._e2b_sandbox is None


def test_unsupported_backend_exec(tmp_path: Path) -> None:
    profile = MicroVMProfile(workspace=tmp_path)
    runner = MicroVMRunner.create(profile, backend="unknown")
    runner._spawned = True

    with pytest.raises(MicroVMUnavailableError, match="Unsupported backend"):
        runner.exec(["ls"])


def test_docker_sock_security_violation_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os.path, "exists", lambda p: True)
    monkeypatch.setattr(os, "access", lambda p, m: True)

    profile = MicroVMProfile(workspace=tmp_path, kernel_image_path="path/with/docker.sock")
    runner = MicroVMRunner.create(profile, backend="firecracker", runner=lambda *a, **kw: subprocess.CompletedProcess([], 0))

    with pytest.raises(RuntimeError, match="Security violation"):
        runner.spawn()



