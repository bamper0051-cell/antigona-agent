"""Regressions for FAIL-CLOSED, OBSERVABLE sandbox isolation (S-ISO-1).

The defect: sandbox runtime selection silently fell back from gVisor (``runsc``,
separate user-space kernel) to ``runc`` (shared host kernel) when ``runsc`` was
not registered.  These tests pin the hardened contract:

* ``runsc`` present            -> execution proceeds and reports ``gvisor``;
* ``runsc`` absent/hidden      -> execution is REFUSED with a specific error and
                                  NO container is started on the weaker runtime;
* escape hatch (default OFF)   -> only when explicitly enabled, logs loudly and
                                  reports ``degraded`` / ``runc``;
* the isolation value TRACKS a simulated runtime change (live probe, not a flag).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest

from antigona.sandbox.runner import (
    ALLOW_RUNC_FALLBACK_ENV,
    DEFAULT_RUNTIME,
    GVISOR_RUNTIME,
    ISOLATION_DEGRADED,
    ISOLATION_GVISOR,
    ISOLATION_REFUSED,
    ISOLATION_RUNC,
    RuntimeProbe,
    SandboxIsolationError,
    ensure_runtime_available,
    fallback_allowed,
    probe_isolation,
    write_isolation_state,
)
from antigona.shell import DockerShellTool, ShellInput


class FakeProc:
    def __init__(self, stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode

    def communicate(self, timeout: int) -> tuple[bytes, bytes]:
        del timeout
        return self.stdout, self.stderr

    def poll(self) -> int:
        return self.returncode

    def terminate(self) -> None:
        pass


def _probe(available: set[str], error: str | None = None):
    def probe(_runner: Any = None) -> RuntimeProbe:
        return RuntimeProbe(frozenset(available), error)

    return probe


def _patch_probe(monkeypatch: pytest.MonkeyPatch, available: set[str], error: str | None = None) -> None:
    monkeypatch.setattr(
        "antigona.sandbox.runner.probe_docker_runtimes", _probe(available, error)
    )


# --- escape hatch default is OFF ---------------------------------------------------


def test_escape_hatch_is_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ALLOW_RUNC_FALLBACK_ENV, raising=False)
    assert fallback_allowed() is False


# --- runsc present -> proceeds and reports gvisor ----------------------------------


def test_runsc_present_executes_and_reports_gvisor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(ALLOW_RUNC_FALLBACK_ENV, raising=False)
    _patch_probe(monkeypatch, {"runc", "runsc"})
    seen: list[list[str]] = []

    def fake_popen(argv: list[str], **_kw: Any) -> FakeProc:
        seen.append(list(argv))
        return FakeProc(b"4.19.0-gvisor\n")

    monkeypatch.setattr("subprocess.Popen", fake_popen)
    tool = DockerShellTool(tmp_path / "ws", runtime=GVISOR_RUNTIME)
    result = tool.execute(ShellInput(("uname", "-r")))
    assert result.ok is True
    assert "gvisor" in str(result.data.get("output", ""))
    assert seen and f"--runtime={GVISOR_RUNTIME}" in seen[0]
    status = probe_isolation(runner=lambda *a, **k: None)  # probe patched above
    assert status.level == ISOLATION_GVISOR


# --- runsc absent -> refuse fail-closed, no container ---------------------------------


def test_runsc_absent_refuses_and_starts_no_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(ALLOW_RUNC_FALLBACK_ENV, raising=False)
    _patch_probe(monkeypatch, {"runc"})
    started: list[Any] = []

    def fake_popen(*args: Any, **_kw: Any) -> FakeProc:
        started.append(args)
        return FakeProc()

    monkeypatch.setattr("subprocess.Popen", fake_popen)
    tool = DockerShellTool(tmp_path / "ws", runtime=GVISOR_RUNTIME)
    result = tool.execute(ShellInput(("uname", "-r")))
    assert result.ok is False
    assert result.error is not None
    assert GVISOR_RUNTIME in result.error
    assert ALLOW_RUNC_FALLBACK_ENV in result.error  # specific, actionable remedy
    assert started == []  # NOTHING ran on the weaker runtime


def test_pre_execution_guard_detects_runtime_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A runsc that disappeared AFTER construction is caught before execution."""
    monkeypatch.delenv(ALLOW_RUNC_FALLBACK_ENV, raising=False)
    _patch_probe(monkeypatch, {"runc"})
    with pytest.raises(SandboxIsolationError):
        ensure_runtime_available(GVISOR_RUNTIME)


def test_runc_runtime_without_hatch_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(ALLOW_RUNC_FALLBACK_ENV, raising=False)
    with pytest.raises(SandboxIsolationError):
        ensure_runtime_available(DEFAULT_RUNTIME)


# --- degraded / override mode: loud + visible ----------------------------------------


def test_degraded_mode_logs_loudly_and_reports_degraded(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv(ALLOW_RUNC_FALLBACK_ENV, "1")
    _patch_probe(monkeypatch, {"runc"})
    caplog.set_level(logging.WARNING, logger="antigona")
    status = probe_isolation()
    assert status.level == ISOLATION_DEGRADED
    assert status.runtime == DEFAULT_RUNTIME
    assert status.kernel_isolated is False
    # The acknowledgement is loud at selection time.
    from antigona.sandbox.runner import select_runtime

    select_runtime("auto", frozenset({"runc"}))
    assert any("DEGRADED" in r.message for r in caplog.records)


def test_explicit_runc_with_hatch_reports_runc(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ALLOW_RUNC_FALLBACK_ENV, "1")
    _patch_probe(monkeypatch, {"runc", "runsc"})
    status = probe_isolation(DEFAULT_RUNTIME)
    assert status.level == ISOLATION_RUNC
    assert status.kernel_isolated is False


# --- the isolation value TRACKS a simulated runtime change ----------------------------


def test_isolation_level_tracks_simulated_runtime_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(ALLOW_RUNC_FALLBACK_ENV, raising=False)
    _patch_probe(monkeypatch, {"runc", "runsc"})
    assert probe_isolation().level == ISOLATION_GVISOR
    # Simulate runsc disappearing (upgrade dropped it / daemon restarted
    # without the runtime) — the value MUST follow the probe, not a config flag.
    _patch_probe(monkeypatch, {"runc"})
    assert probe_isolation().level == ISOLATION_REFUSED
    # Simulate the daemon itself becoming unreachable.
    _patch_probe(monkeypatch, set(), "docker daemon unreachable (connection refused)")
    assert probe_isolation().level == ISOLATION_REFUSED


def test_isolation_state_persists_live_level(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv(ALLOW_RUNC_FALLBACK_ENV, raising=False)
    state = tmp_path / "sandbox_isolation.json"
    monkeypatch.setenv("ANTIGONA_SANDBOX_ISOLATION_STATE", str(state))
    _patch_probe(monkeypatch, {"runc", "runsc"})
    written = write_isolation_state(probe_isolation())
    assert written == state
    payload = json.loads(state.read_text(encoding="utf-8"))
    assert payload["level"] == ISOLATION_GVISOR
    assert payload["kernel_isolated"] is True


def test_health_isolation_helper_reports_probed_level(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The value surfaced by /health is the LIVE probe, not configuration."""
    monkeypatch.delenv(ALLOW_RUNC_FALLBACK_ENV, raising=False)
    monkeypatch.setenv("ANTIGONA_SANDBOX_ISOLATION_STATE", str(tmp_path / "iso.json"))
    from antigona.gateway.api import _sandbox_isolation

    _patch_probe(monkeypatch, {"runc", "runsc"})
    assert _sandbox_isolation().level == ISOLATION_GVISOR
    _patch_probe(monkeypatch, {"runc"})
    assert _sandbox_isolation().level == ISOLATION_REFUSED


# --- the other sandboxed-command path (DockerSandboxBackend) --------------------------


def test_docker_sandbox_backend_argv_has_explicit_runtime(tmp_path: Path) -> None:
    from antigona.sandbox.docker_sandbox import DockerSandboxBackend

    backend = DockerSandboxBackend(workspace=str(tmp_path), runtime=GVISOR_RUNTIME)
    argv = backend._build_argv(["echo", "hi"])
    assert f"--runtime={GVISOR_RUNTIME}" in argv


def test_docker_sandbox_backend_refuses_without_gvisor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from antigona.sandbox.docker_sandbox import DockerSandboxBackend

    monkeypatch.delenv(ALLOW_RUNC_FALLBACK_ENV, raising=False)
    _patch_probe(monkeypatch, {"runc"})
    backend = DockerSandboxBackend(workspace=str(tmp_path), runtime=GVISOR_RUNTIME)
    monkeypatch.setattr(backend, "is_available", lambda: True)
    with pytest.raises(SandboxIsolationError):
        backend.run(["echo", "hi"])
