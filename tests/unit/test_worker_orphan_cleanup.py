"""Unit tests for worker orphaned-sandbox-container cleanup.

A worker killed mid-flow leaves a throwaway ``antigona-*`` gVisor container
behind; without cleanup a re-execution inherits its (possibly interrupted)
non-zero exit code and the flow hard-fails. The startup sweep removes them so
orphaned flows re-execute fresh.
"""
import subprocess

from antigona.worker import _cleanup_orphaned_sandbox_containers


def test_cleanup_removes_orphaned_containers(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(argv, *a, **k):
        calls.append(list(argv))
        if argv[:2] == ["docker", "ps"]:
            return subprocess.CompletedProcess(argv, 0, stdout="abc123\ndef456\n")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    _cleanup_orphaned_sandbox_containers()
    rm = [c for c in calls if c[:2] == ["docker", "rm"]]
    assert len(rm) == 2, rm
    assert rm[0][2:] == ["-f", "abc123"]
    assert rm[1][2:] == ["-f", "def456"]


def test_cleanup_noop_when_no_containers(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(argv, *a, **k):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout="\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    _cleanup_orphaned_sandbox_containers()
    rm = [c for c in calls if c[:2] == ["docker", "rm"]]
    assert rm == []


def test_cleanup_survives_docker_error(monkeypatch) -> None:
    def fake_run(argv, *a, **k):
        if argv[:2] == ["docker", "ps"]:
            raise FileNotFoundError("docker missing")
        raise AssertionError("rm should not be reached")

    monkeypatch.setattr(subprocess, "run", fake_run)
    _cleanup_orphaned_sandbox_containers()  # must not raise
