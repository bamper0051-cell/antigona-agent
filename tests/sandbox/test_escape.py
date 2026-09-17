"""Escape tests: prove the fail-closed profile actually contains a hostile tool.

These run real containers under the resolved runtime (gVisor when registered,
otherwise runc). They skip cleanly where Docker is unavailable so the suite
stays green in hermetic environments; the profile-shape guarantees are covered
unconditionally in ``test_runner.py``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from antigona.sandbox.runner import (
    GVISOR_RUNTIME,
    SandboxProfile,
    SandboxRunner,
    docker_available,
    resolve_runtime,
    runtime_registered,
)

ESCAPE_IMAGE = "alpine:latest"


def _image_present() -> bool:
    try:
        proc = subprocess.run(
            ["docker", "image", "inspect", ESCAPE_IMAGE],
            capture_output=True,
            timeout=15,
            check=False,
        )
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


pytestmark = pytest.mark.skipif(
    not (docker_available() and _image_present()),
    reason="docker daemon or alpine image unavailable",
)


def _runner(tmp_path: Path, *, pids_limit: int = 64, memory: str = "128m") -> SandboxRunner:
    runtime = resolve_runtime("auto")
    profile = SandboxProfile(
        workspace=tmp_path,
        image=ESCAPE_IMAGE,
        runtime=runtime,
        pids_limit=pids_limit,
        memory=memory,
    )
    return SandboxRunner(profile)


def test_runs_under_gvisor(tmp_path: Path) -> None:
    if not runtime_registered(GVISOR_RUNTIME):
        pytest.skip("runsc runtime not registered in docker")
    proc = _runner(tmp_path).run(["uname", "-r"], timeout=60)
    assert proc.returncode == 0
    assert "gvisor" in proc.stdout.lower(), proc.stdout


def test_network_escape_blocked(tmp_path: Path) -> None:
    # --network=none: no route off the host, any egress attempt fails.
    proc = _runner(tmp_path).run(
        ["wget", "-T", "3", "-q", "-O", "/dev/null", "http://example.com"],
        timeout=60,
    )
    assert proc.returncode != 0, "network egress unexpectedly succeeded"


def test_filesystem_escape_blocked(tmp_path: Path) -> None:
    runner = _runner(tmp_path)
    # Read-only root + only the workspace mounted rw: writing outside fails.
    outside = runner.run(["sh", "-c", "echo pwned > /etc/antigona_escape"], timeout=60)
    assert outside.returncode != 0, "wrote outside workspace despite read-only root"
    assert not Path("/etc/antigona_escape").exists()
    # Positive control: the workspace itself is writable.
    inside = runner.run(["sh", "-c", "echo ok > /workspace/inside.txt"], timeout=60)
    assert inside.returncode == 0, inside.stderr
    assert (tmp_path / "inside.txt").read_text().strip() == "ok"


def test_fork_bomb_contained_by_pids_limit(tmp_path: Path) -> None:
    # A low --pids-limit that still lets the (gVisor) sandbox boot, but caps a
    # fork bomb. Memory is generous so PIDs are the binding constraint. An
    # *uncontained* run would exit 0 and print "SPAWNED=400"; containment shows
    # up as a non-zero exit (gVisor tears the sandbox down under PID pressure)
    # and/or an explicit fork-failure message (runc: "can't fork: Resource
    # temporarily unavailable", gVisor: "Out of memory" for the same EAGAIN).
    runner = _runner(tmp_path, pids_limit=32, memory="256m")
    proc = runner.run(
        ["sh", "-c", "i=0; while [ $i -lt 400 ]; do sleep 5 & i=$((i+1)); done; echo SPAWNED=$i"],
        timeout=90,
    )
    combined = (proc.stdout + proc.stderr).lower()
    fork_denied = (
        "can't fork" in combined
        or "resource temporarily unavailable" in combined
        or "out of memory" in combined
    )
    ran_to_completion = proc.returncode == 0 and "spawned=400" in combined
    assert not ran_to_completion, f"fork bomb was NOT contained: {combined!r}"
    assert proc.returncode != 0 or fork_denied, (
        f"no containment signal (rc={proc.returncode}, out={combined!r})"
    )
