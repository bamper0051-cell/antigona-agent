from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from antigona.shell import DockerShellTool, ShellInput


class FakePopen:
    def __init__(self, returncode: int = 0, stdout: bytes = b"", stderr: bytes = b"", poll: int | None = 0) -> None:
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr
        self._poll = poll

    def communicate(self, timeout: int) -> tuple[bytes, bytes]:
        del timeout
        return self._stdout, self._stderr

    def poll(self) -> int | None:
        return self._poll

    def terminate(self) -> None:
        self._poll = None


@dataclass
class RunResult:
    returncode: int = 0
    stdout: bytes = b""
    stderr: bytes = b""
    poll: int | None = 0


def patch_popen(monkeypatch: pytest.MonkeyPatch, result: RunResult) -> None:
    def fake_popen(*_a: Any, **_k: Any) -> FakePopen:
        return FakePopen(result.returncode, result.stdout, result.stderr, result.poll)
    monkeypatch.setattr("subprocess.Popen", fake_popen)


def test_existing_container_recovered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], *a: Any, **k: Any) -> object:
        calls.append(cmd)
        stdout = "recovered named container" if "logs" in cmd else ("0" if "--format={{.State.ExitCode}}" in cmd else "")
        obj = type("R", (), {"returncode": 0, "stdout": stdout, "stderr": ""})()
        return obj

    monkeypatch.setattr("subprocess.run", fake_run)
    patch_popen(monkeypatch, RunResult(stdout=b"recovered"))
    tool = DockerShellTool(tmp_path, timeout_seconds=5)
    result = tool.execute(ShellInput(("true",), "exec-1234"))
    assert result.ok and result.data["output"] == "recovered named container"
    assert calls and calls[0][0] == "docker" and calls[0][1] == "inspect"


def test_tool_timeout_triggers_cancel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    def fake_popen(*_a: Any, **_k: Any) -> FakePopen:
        def communicate(timeout: int) -> tuple[bytes, bytes]:
            del timeout
            raise subprocess.TimeoutExpired("docker", 5)
        obj = FakePopen()
        obj.communicate = communicate  # type: ignore[method-assign]
        return obj
    monkeypatch.setattr("subprocess.Popen", fake_popen)

    def fake_run(*_a: Any, **_k: Any) -> object:
        # The adapter's sticky-cancel path shells out to `docker kill`; the cancel
        # must complete without invoking a real binary in the network-free test.
        return type("R", (), {"returncode": 0})()

    monkeypatch.setattr("subprocess.run", fake_run)
    tool = DockerShellTool(tmp_path, timeout_seconds=1)
    result = tool.execute(ShellInput(("sleep", "9")))
    assert not result.ok and result.error == "tool timeout" and result.retryable


def test_sandbox_unavailable_file_not_found(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_popen(*_a: Any, **_k: Any) -> None:
        raise FileNotFoundError("docker: no such file")
    monkeypatch.setattr("subprocess.Popen", fake_popen)
    tool = DockerShellTool(tmp_path)
    result = tool.execute(ShellInput(("true",)))
    assert not result.ok and result.error is not None and "sandbox unavailable" in result.error


def test_empty_command_rejected(tmp_path: Path) -> None:
    tool = DockerShellTool(tmp_path)
    assert tool.execute(ShellInput(())).error == "empty command"


def test_cancel_kills_container(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    killed: list[list[str]] = []

    def fake_run(cmd: list[str], *a: Any, **k: Any) -> object:
        killed.append(cmd)
        return type("R", (), {"returncode": 0})()
    monkeypatch.setattr("subprocess.run", fake_run)
    tool = DockerShellTool(tmp_path)
    tool._container_name = "antigona-xyz"
    tool.cancel()
    assert killed and killed[0][0] == "docker" and killed[0][1] == "kill"


def test_no_existing_container_runs_new_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: with an execution_id but NO recovered container, execute() must
    fall through and run a NEW container (docker run via Popen), NOT fail closed with
    EXECUTION_UNKNOWN. Guard against the hermes/reinstall-ci regression that added a
    premature ``return self._failure("EXECUTION_UNKNOWN ...")`` when recovery returned None,
    which broke every sandbox.shell task on first run.
    """
    run_calls: list[list[str]] = []

    def fake_run(cmd: list[str], *a: Any, **k: Any) -> object:
        run_calls.append(cmd)
        # docker inspect <name> -> non-zero returncode => container does NOT exist
        if cmd[:2] == ["docker", "inspect"]:
            return type("R", (), {"returncode": 1})()
        return type("R", (), {"returncode": 0})()

    monkeypatch.setattr("subprocess.run", fake_run)
    # Popen fake: docker run <name> ... -> writes stdout, exit 0
    monkeypatch.setattr(
        "subprocess.Popen",
        lambda *_a, **_k: FakePopen(returncode=0, stdout=b"edge-tts installed\n"),
    )
    tool = DockerShellTool(tmp_path, timeout_seconds=5)
    result = tool.execute(ShellInput(("pip", "install", "edge-tts"), "exec-nofix"))
    assert result.ok, f"expected fall-through to new container, got: {result.error!r}"
    assert result.data["output"] == "edge-tts installed"
    # recovery was attempted (inspect), then a real run was dispatched via Popen
    assert run_calls and run_calls[0][:2] == ["docker", "inspect"]
    assert tool._container_name is None  # reset in finally


def test_system_install_maps_git_to_apk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []
    monkeypatch.setattr("subprocess.run", lambda cmd, *a, **k: (seen.append(cmd) or type("R", (), {"returncode": 1})()))
    monkeypatch.setattr("subprocess.Popen", lambda cmd, **k: (seen.append(cmd) or FakePopen(returncode=0)))
    result = DockerShellTool(tmp_path, image="python:3.12-alpine").execute(ShellInput(("pip install git",)))
    assert result.ok
    assert any("apk" in cmd and "git" in cmd for cmd in seen)


def test_nonzero_result_keeps_safe_evidence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    patch_popen(monkeypatch, RunResult(returncode=17, stderr=b"SECRET_TOKEN=bad permission denied"))
    result = DockerShellTool(tmp_path).execute(ShellInput(("git",)))
    assert not result.ok
    assert [(e.kind, e.value) for e in result.evidence] == [("returncode", "17"), ("diagnostic", "permission_denied")]
    assert "SECRET" not in repr(result) and "bad" not in repr(result)
    assert DockerShellTool.requires_approval is True


def test_system_install_maps_exact_legacy_sh_c_form(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []
    monkeypatch.setattr("subprocess.run", lambda cmd, *a, **k: (seen.append(cmd) or type("R", (), {"returncode": 1})()))
    monkeypatch.setattr("subprocess.Popen", lambda cmd, **k: (seen.append(cmd) or FakePopen(returncode=0)))
    result = DockerShellTool(tmp_path, image="debian:bookworm").execute(
        ShellInput(("sh", "-c", "pip install git"))
    )
    assert result.ok
    assert any("apt-get" in cmd and "git" in cmd for cmd in seen)


@pytest.mark.parametrize("payload", [
    "pip install git && touch /workspace/pwned",
    "pip install git;id",
    "pip install $(id)",
    "pip install 'git'",
    "pip install git extra",
    "pip install unknown",
])
def test_system_install_rejects_unsafe_or_unsupported_legacy_sh_c(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, payload: str
) -> None:
    seen: list[list[str]] = []
    monkeypatch.setattr("subprocess.run", lambda cmd, *a, **k: (seen.append(cmd) or type("R", (), {"returncode": 1})()))
    monkeypatch.setattr("subprocess.Popen", lambda cmd, **k: (seen.append(cmd) or FakePopen(returncode=0)))
    result = DockerShellTool(tmp_path, image="python:3.12-alpine").execute(
        ShellInput(("sh", "-c", payload))
    )
    assert result.ok
    assert not any("apk" in cmd or "apt-get" in cmd for cmd in seen)
    assert any(cmd[-3:] == ["sh", "-c", payload] for cmd in seen)
