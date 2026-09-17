"""Regression tests for CLI executor prompt ordering and containment auth mounting."""

import sys
from pathlib import Path

import pytest

from antigona.orchestration.executors import ServiceExecutors


def test_claude_adapter_prompt_argument_position() -> None:
    """Verify that Claude CLI receives instruction argument before variadic --allowedTools."""
    instruction = "echo test_instruction"
    cmd = ["claude", "-p", "--bare", "--no-session-persistence", instruction, "--allowedTools", "Read,Glob,Grep"]
    
    assert cmd[4] == instruction
    assert cmd[5] == "--allowedTools"


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="bubblewrap unavailable on Windows; fail-closed refusal is correct (Wave 4)",
)
@pytest.mark.asyncio
async def test_codex_confined_credential_mounting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify Codex credential file read-only binding into bwrap container."""
    ws = tmp_path / "ws"
    ws.mkdir()
    
    # Create fake auth files to trigger mounting
    fake_codex = tmp_path / ".codex"
    fake_codex.mkdir()
    auth_file = fake_codex / "auth.json"
    auth_file.write_text("{}")

    # D9: the credential source is now derived from the canonical home helper
    # (antigona.core.paths.home_dir(), overridable via ANTIGONA_HOME_DIR) instead
    # of a hardcoded "~/.codex/auth.json" literal. Point the helper at a fake
    # home so the unit test never touches a real server path.
    monkeypatch.setenv("ANTIGONA_HOME_DIR", str(tmp_path))

    from antigona.orchestration.autonomy import SANDBOX_HOME

    bwrap_cmd_called = []
    
    def fake_run(cmd, *args, **kwargs):
        bwrap_cmd_called.extend(cmd)
        import subprocess
        return subprocess.CompletedProcess(cmd, 0, "test_codex_confined_unit\n", "")
        
    monkeypatch.setattr("subprocess.run", fake_run)

    execs = ServiceExecutors(timeout=30)
    res = execs.execute("codex", "echo test_codex_confined_unit", workspace=str(ws), writable=False)
    
    assert res.ok is True
    assert "test_codex_confined_unit" in res.output
    assert "--ro-bind" in bwrap_cmd_called
    # Assert auth file was mounted
    idx = bwrap_cmd_called.index(str(auth_file))
    assert bwrap_cmd_called[idx+1] == f"{SANDBOX_HOME}/.codex/auth.json"
