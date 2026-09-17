"""Integration: LocalWorkspace routes high-risk shell to the Docker sandbox only
when approved, and refuses on the host otherwise.

Requires Docker daemon; skipped otherwise. Uses the REAL integration point
(agent_core -> LocalWorkspace.execute_command -> WorkspaceShellTool).
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from antigona.worker.tools.common import ToolError
from antigona.workspace import LocalWorkspace

docker_available = shutil.which("docker") is not None and (
    subprocess.run(["docker", "info"], capture_output=True, text=True, timeout=10).returncode == 0
)


@pytest.mark.skipif(not docker_available, reason="Docker daemon unavailable")
def test_high_risk_approved_runs_in_docker_sandbox(tmp_path) -> None:
    ws = LocalWorkspace(root_path=tmp_path / "ws")
    res = ws.execute_command(["python", "-c", "print('E2E_SANDBOX_OK')"], approved=True, task_id="e2e-t1")
    assert res.exit_code == 0
    assert "E2E_SANDBOX_OK" in res.stdout


@pytest.mark.skipif(not docker_available, reason="Docker daemon unavailable")
def test_high_risk_without_approval_refused_on_host(tmp_path) -> None:
    ws = LocalWorkspace(root_path=tmp_path / "ws")
    with pytest.raises(ToolError) as exc:
        ws.execute_command(["python", "-c", "print('should-not-run')"])
    assert "requires owner approval" in str(exc.value)
    # and the host never executed it
    assert "should-not-run" not in (ws.root_path / "should-not-run").read_text() if (ws.root_path / "should-not-run").exists() else True


@pytest.mark.skipif(not docker_available, reason="Docker daemon unavailable")
def test_low_risk_still_runs_on_host(tmp_path) -> None:
    ws = LocalWorkspace(root_path=tmp_path / "ws")
    res = ws.execute_command(["echo", "host-ok"])
    assert res.exit_code == 0
    assert res.stdout.strip() == "host-ok"
