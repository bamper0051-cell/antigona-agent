from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any, cast

import pytest

from antigona.sandbox.runner import (
    ALLOW_RUNC_FALLBACK_ENV,
    DEFAULT_RUNTIME,
    GVISOR_RUNTIME,
    SandboxIsolationError,
    SandboxProfile,
    SandboxRunner,
    build_run_argv,
    docker_available,
    docker_runtimes,
    resolve_runtime,
    runtime_registered,
    select_runtime,
)


class FakeProc:
    def __init__(self, stdout: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.returncode = returncode


def _runner(stdout: str = "", returncode: int = 0, raises: BaseException | None = None) -> Any:
    def run(*_a: Any, **_k: Any) -> FakeProc:
        if raises is not None:
            raise raises
        return FakeProc(stdout, returncode)
    return run


def _silent(*_a: Any, **_k: Any) -> None:
    return None


# --- docker_runtimes / availability -------------------------------------------------


def test_docker_runtimes_parses_json() -> None:
    runner = _runner('{"runc":{},"runsc":{}}')
    assert docker_runtimes(runner) == frozenset({"runc", "runsc"})
    assert runtime_registered("runsc", runner)
    assert docker_available(runner)


def test_docker_runtimes_failclosed_on_nonzero() -> None:
    assert docker_runtimes(_runner("{}", returncode=1)) == frozenset()


def test_docker_runtimes_failclosed_on_bad_json() -> None:
    assert docker_runtimes(_runner("not-json")) == frozenset()


def test_docker_runtimes_failclosed_on_non_dict() -> None:
    assert docker_runtimes(_runner("[1,2,3]")) == frozenset()


def test_docker_runtimes_failclosed_when_no_binary() -> None:
    assert docker_runtimes(_runner(raises=FileNotFoundError("docker"))) == frozenset()
    assert not docker_available(_runner(raises=FileNotFoundError("docker")))


# --- select_runtime -----------------------------------------------------------------


def test_runsc_selected_when_available() -> None:
    assert select_runtime("auto", frozenset({"runc", "runsc"}), log=_silent) == GVISOR_RUNTIME
    assert select_runtime("runsc", frozenset({"runsc"}), log=_silent) == GVISOR_RUNTIME


def test_refuses_failclosed_when_runsc_missing(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No runsc and no escape hatch -> REFUSE, never a silent runc downgrade."""
    monkeypatch.delenv(ALLOW_RUNC_FALLBACK_ENV, raising=False)
    caplog.set_level(logging.ERROR, logger="antigona")
    with pytest.raises(SandboxIsolationError) as exc:
        select_runtime("auto", frozenset({"runc"}), log=_silent)
    message = str(exc.value)
    assert GVISOR_RUNTIME in message
    assert ALLOW_RUNC_FALLBACK_ENV in message  # remedy names the escape hatch
    assert "REFUSED" in message
    assert any(r.levelno == logging.ERROR for r in caplog.records)


def test_refuses_when_docker_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ALLOW_RUNC_FALLBACK_ENV, raising=False)
    with pytest.raises(SandboxIsolationError):
        select_runtime("auto", frozenset(), probe_error="daemon down", log=_silent)


def test_degraded_fallback_logs_loudly_when_escape_hatch_enabled(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_RUNC_FALLBACK_ENV, "1")
    caplog.set_level(logging.WARNING, logger="antigona")
    chosen = select_runtime("auto", frozenset({"runc"}), log=_silent)
    assert chosen == DEFAULT_RUNTIME
    assert any(r.levelno == logging.WARNING and "DEGRADED" in r.message for r in caplog.records)


def test_explicit_runc_preference_requires_escape_hatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(ALLOW_RUNC_FALLBACK_ENV, raising=False)
    with pytest.raises(SandboxIsolationError):
        select_runtime("runc", frozenset({"runc", "runsc"}), log=_silent)


def test_explicit_runc_preference_warns_when_hatch_enabled(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_RUNC_FALLBACK_ENV, "1")
    caplog.set_level(logging.WARNING, logger="antigona")
    chosen = select_runtime("runc", frozenset({"runc", "runsc"}), log=_silent)
    assert chosen == DEFAULT_RUNTIME
    assert any("DISABLED" in r.message for r in caplog.records)


def test_unknown_preference_falls_back_to_autodetect(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="antigona")
    assert select_runtime("banana", frozenset({"runsc"}), log=_silent) == GVISOR_RUNTIME
    assert any("unknown sandbox runtime" in r.message for r in caplog.records)


def test_resolve_runtime_queries_docker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ALLOW_RUNC_FALLBACK_ENV, raising=False)
    assert resolve_runtime("auto", runner=_runner('{"runsc":{}}'), log=_silent) == GVISOR_RUNTIME
    with pytest.raises(SandboxIsolationError):
        resolve_runtime("auto", runner=_runner('{"runc":{}}'), log=_silent)


# --- build_run_argv fail-closed profile --------------------------------------------


def test_argv_contains_full_failclosed_profile(tmp_path: Path) -> None:
    profile = SandboxProfile(workspace=tmp_path, image="python:3.12-alpine", runtime=GVISOR_RUNTIME)
    argv = build_run_argv(profile, ("echo", "hi"), name="antigona-x")
    assert argv[:2] == ["docker", "run"]
    assert f"--runtime={GVISOR_RUNTIME}" in argv
    assert "--network=none" in argv
    assert "--read-only" in argv
    assert "--cap-drop=ALL" in argv
    assert "--security-opt=no-new-privileges" in argv
    assert any(a.startswith("--user=") for a in argv)
    assert "--memory=128m" in argv
    assert "--cpus=0.5" in argv
    assert "--pids-limit=64" in argv
    assert "--stop-timeout=30" in argv
    assert "--tmpfs" in argv
    assert f"{tmp_path.resolve()}:/workspace:rw" in argv
    assert argv[-2:] == ["echo", "hi"]
    assert "--name" in argv and "antigona-x" in argv
    # Never leak the docker socket into a tool container.
    assert not any("docker.sock" in a for a in argv)


def test_argv_interactive_flag(tmp_path: Path) -> None:
    profile = SandboxProfile(workspace=tmp_path, runtime=DEFAULT_RUNTIME)
    argv = build_run_argv(profile, ("cat",), interactive=True)
    assert "-i" in argv
    assert f"--runtime={DEFAULT_RUNTIME}" in argv


def test_argv_install_capable_profile_has_network_and_writable(tmp_path: Path) -> None:
    # Install-capable shell profile: bridge network + writable root + the caps
    # apt needs under --cap-drop=ALL + enough memory for dpkg.
    profile = SandboxProfile(
        workspace=tmp_path,
        image="python:3.12-slim",
        network="bridge",
        read_only=False,
        caps_add=("DAC_OVERRIDE", "FOWNER", "SETGID", "SETUID"),
        memory="1g",
    )
    argv = build_run_argv(profile, ("pip", "install", "uv"))
    assert "--network=bridge" in argv
    assert "--read-only" not in argv
    assert "--memory=1g" in argv
    for cap in ("DAC_OVERRIDE", "FOWNER", "SETGID", "SETUID"):
        assert f"--cap-add={cap}" in argv
    # Still fail-closed on the rest of the hardening surface.
    assert "--cap-drop=ALL" in argv
    assert "--security-opt=no-new-privileges" in argv
    assert not any("docker.sock" in a for a in argv)


def test_argv_default_remains_fail_closed_after_install_profile(tmp_path: Path) -> None:
    # Adding the install-capable profile must NOT weaken the default.
    profile = SandboxProfile(workspace=tmp_path)
    argv = build_run_argv(profile, ("echo", "hi"))
    assert "--network=none" in argv
    assert "--read-only" in argv
    assert "--memory=128m" in argv
    assert not any(a.startswith("--cap-add=") for a in argv)


@pytest.mark.skipif(sys.platform == "win32", reason='POSIX st_mode bits (0o600/0o640/0o750) not enforceable on Windows (Wave 4)')
def test_profile_hardened_workspace_enforces_0750(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    profile = SandboxProfile(workspace=ws, runtime=DEFAULT_RUNTIME)
    resolved = profile.hardened_workspace()
    assert resolved.exists()
    assert (resolved.stat().st_mode & 0o777) == 0o750


# --- SandboxRunner -------------------------------------------------------------------


def test_runner_create_resolves_and_runs(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def exec_runner(argv: list[str], **_k: Any) -> FakeProc:
        calls.append(argv)
        return FakeProc("done", 0)

    runner = SandboxRunner.create(
        tmp_path,
        image="alpine:latest",
        preferred_runtime="auto",
        runner=_runner('{"runsc":{}}'),
        log=_silent,
    )
    assert runner.runtime == GVISOR_RUNTIME
    assert runner.under_gvisor()
    proc = runner.run(["echo", "hi"], runner=cast(Any, exec_runner))
    assert proc.stdout == "done"
    assert calls and calls[0][0] == "docker" and "--runtime=runsc" in calls[0]


def test_runner_refuses_without_gvisor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ALLOW_RUNC_FALLBACK_ENV, raising=False)
    with pytest.raises(SandboxIsolationError):
        SandboxRunner.create(
            tmp_path,
            preferred_runtime="auto",
            runner=_runner('{"runc":{}}'),
            log=_silent,
        )


def test_runner_uses_runc_only_with_escape_hatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_RUNC_FALLBACK_ENV, "1")
    runner = SandboxRunner.create(
        tmp_path,
        preferred_runtime="auto",
        runner=_runner('{"runc":{}}'),
        log=_silent,
    )
    assert runner.runtime == DEFAULT_RUNTIME
    assert not runner.under_gvisor()
