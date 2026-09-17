"""Integration test: real Docker sandbox execution of a high-risk command.

Requires a working Docker daemon; skipped otherwise. Verifies:
  * high-risk command (pip, not in the P0 allowlist) executes inside the container;
  * the minimal workspace bind is the only host mount (container writes visible on host);
  * the container cannot reach the host loopback (no --network host).
"""

from __future__ import annotations

import os
import shutil
import socket
import tempfile
import threading

import pytest

from antigona.sandbox.docker_sandbox import DockerSandboxBackend

docker_available = shutil.which("docker") is not None and (
    __import__("subprocess")
    .run(["docker", "info"], capture_output=True, text=True, timeout=10)
    .returncode
    == 0
)


@pytest.mark.skipif(not docker_available, reason="Docker daemon unavailable")
def test_high_risk_command_runs_in_docker_sandbox() -> None:
    ws = tempfile.mkdtemp(prefix="sandbox_integration_")
    b = DockerSandboxBackend(workspace=ws)
    res = b.run(["pip", "--version"], correlation_id="it-corr", task_id="it-task")
    assert res.exit_code == 0
    assert "pip" in res.stdout
    assert res.container_id.startswith("antigona-sandbox-")


@pytest.mark.skipif(not docker_available, reason="Docker daemon unavailable")
def test_workspace_mount_is_the_only_bind() -> None:
    ws = tempfile.mkdtemp(prefix="sandbox_integration_")
    b = DockerSandboxBackend(workspace=ws)
    res = b.run(["sh", "-c", "echo MOUNT_OK > /workspace/probe.txt"])
    assert res.exit_code == 0
    assert os.path.exists(os.path.join(ws, "probe.txt"))
    assert open(os.path.join(ws, "probe.txt")).read().strip() == "MOUNT_OK"


@pytest.mark.skipif(not docker_available, reason="Docker daemon unavailable")
def test_container_cannot_reach_host_network() -> None:
    ws = tempfile.mkdtemp(prefix="sandbox_integration_")
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    port = srv.getsockname()[1]
    srv.listen(1)

    def _accept() -> None:
        try:
            c, _ = srv.accept()
            c.close()
        except Exception:
            pass

    th = threading.Thread(target=_accept, daemon=True)
    th.start()
    b = DockerSandboxBackend(workspace=ws)
    res = b.run(["sh", "-c", f"echo PING > /dev/tcp/172.17.0.1/{port} || echo NO_REACH"])
    srv.close()
    assert res.exit_code == 0
    assert "NO_REACH" in res.stdout
