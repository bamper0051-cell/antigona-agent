"""B22 regression: remaining hardcoded home literals are derived, not fixed.

Defect (B22): functional production code still hardcoded the literal ``/opt/antigona-home``
(the canonical host's home directory) in defaults and, more seriously, in
workspace deny-lists. The release-gate regexes only match a ``/opt/antigona-home/`` prefix,
so a *bare* ``/opt/antigona-home`` slipped into the shipped artifact. Two consequences:

* **portability** — on a host where ``HOME`` / ``ANTIGONA_HOME_DIR`` differs, a
  default pointed at a directory that does not exist (the terminal tool's
  default root, the ``df`` quick command, the legacy status-server argv, the
  read-only system prompt's workspace line);
* **security** — the workspace deny-lists in ``antigona.task.runtime`` and
  ``antigona.tools.filesystem_read`` denied ``/opt/antigona-home`` instead of the *real*
  home, so on such a host the actual home directory was NOT denied and a task
  workspace could be rooted at the user's home.

Every fixed site must take the home path from the single resolver
``antigona.core.paths.home_dir()`` (ADR-007 — one source of truth, honours
``ANTIGONA_HOME_DIR``). Each test below fails on the pre-fix payload parent
(``HEAD~``) and passes after the fix; none is skipped or xfailed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from antigona.core import paths


def _run_probe(code: str, extra_env: dict[str, str]) -> dict:
    """Import a module under an overridden env in a fresh interpreter.

    Module-level constants (``_QUICK_COMMANDS``, ``FORBIDDEN_SOURCE_PATTERNS``)
    are frozen at import, so they must be observed the same way the released
    process observes them.
    """
    env = dict(os.environ)
    env.update(extra_env)
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(paths.project_root()),
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(proc.stdout.strip().splitlines()[-1])


# ── 1. terminal tool default root ────────────────────────────────────────────


def test_terminal_default_root_follows_overridden_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``TerminalTool`` default root is the real home, not the ``/opt/antigona-home`` literal."""
    from antigona.tools.terminal import TerminalTool

    fake_home = tmp_path / "fakehome_b22_terminal"
    fake_home.mkdir()
    monkeypatch.delenv("ANTIGONA_WORKSPACE", raising=False)
    monkeypatch.setenv("ANTIGONA_HOME_DIR", str(fake_home))

    tool = TerminalTool()
    assert tool._root == fake_home.resolve()
    assert tool._root != Path("/opt/antigona-home")


def test_terminal_explicit_boundary_and_env_still_win(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fix must not change the explicit-boundary / ``ANTIGONA_WORKSPACE`` priority."""
    from antigona.tools.terminal import TerminalTool

    monkeypatch.setenv("ANTIGONA_HOME_DIR", str(tmp_path / "fake_home_unused"))
    ws = tmp_path / "ws_env"
    ws.mkdir()
    monkeypatch.setenv("ANTIGONA_WORKSPACE", str(ws))
    assert TerminalTool()._root == ws.resolve()

    explicit = tmp_path / "ws_explicit"
    explicit.mkdir()
    assert TerminalTool(root_boundary=str(explicit))._root == explicit.resolve()


# ── 2. task runtime workspace deny-list (security) ───────────────────────────


def test_task_runtime_denies_real_home_not_literal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The action-workspace deny-list rejects the *real* home directory."""
    from antigona.task.runtime import PathBoundaryViolation, _validate_action_workspace

    fake_home = tmp_path / "fakehome_b22_runtime"
    fake_home.mkdir()
    monkeypatch.setenv("ANTIGONA_HOME_DIR", str(fake_home))

    with pytest.raises(PathBoundaryViolation):
        _validate_action_workspace(fake_home)
    with pytest.raises(PathBoundaryViolation):
        _validate_action_workspace("/")

    ok = tmp_path / "ws_ok"
    ok.mkdir()
    assert _validate_action_workspace(ok) == ok.resolve()


# ── 3. filesystem read tool deny-list (security) ─────────────────────────────


def test_filesystem_read_denies_real_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``FilesystemReadTool`` refuses the real home, both as explicit root and via env."""
    from antigona.tools.filesystem_read import FilesystemReadTool

    fake_home = tmp_path / "fakehome_b22_read"
    fake_home.mkdir()
    monkeypatch.setenv("ANTIGONA_HOME_DIR", str(fake_home))
    monkeypatch.delenv("ANTIGONA_WORKSPACE", raising=False)

    with pytest.raises(ValueError, match="Invalid workspace root boundary"):
        FilesystemReadTool(root_boundary=str(fake_home))

    monkeypatch.setenv("ANTIGONA_WORKSPACE", str(fake_home))
    with pytest.raises(ValueError, match="Invalid workspace root boundary"):
        FilesystemReadTool()


# ── 4. quick command ``df`` ──────────────────────────────────────────────────


def test_quick_command_df_uses_real_home(tmp_path: Path) -> None:
    """The ``df`` quick command reports the resolver's home, not the literal."""
    fake_home = tmp_path / "fakehome_b22_df"
    fake_home.mkdir()
    out = _run_probe(
        "import json;"
        "from antigona.tools.quick_commands import get_quick_command;"
        "print(json.dumps(get_quick_command('df')[1]))",
        {"ANTIGONA_HOME_DIR": str(fake_home)},
    )
    assert str(fake_home) in out
    assert "/opt/antigona-home" not in out


# ── 5. provenance guard forbidden source patterns ────────────────────────────


def test_provenance_forbidden_patterns_follow_home(tmp_path: Path) -> None:
    """Forbidden legacy source patterns are rooted at the resolver's home."""
    fake_home = tmp_path / "fakehome_b22_prov"
    fake_home.mkdir()
    fake_src = str(fake_home / ".antigona" / "src")
    code = (
        "import json, sys;"
        "from antigona.core.provenance_guard import ("
        "FORBIDDEN_SOURCE_PATTERNS as P, check_forbidden_paths);"
        f"sys.path.append({fake_src!r});"
        "print(json.dumps({'patterns': list(P), 'ok': check_forbidden_paths().ok}))"
    )
    out = _run_probe(code, {"ANTIGONA_HOME_DIR": str(fake_home)})
    assert out["patterns"]
    assert all(p.startswith(str(fake_home)) for p in out["patterns"])
    # the overridden home's legacy copy is actually detected (fail closed)
    assert out["ok"] is False


# ── 6. legacy status-server exemption ────────────────────────────────────────


def test_validator_legacy_status_follows_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_is_legacy_status`` recognises the status server under the real home."""
    from antigona.startup import validator

    fake_home = tmp_path / "fakehome_b22_status"
    fake_home.mkdir()
    monkeypatch.setenv("ANTIGONA_HOME_DIR", str(fake_home))

    derived = validator.ProcInfo(
        1, 1, 0, f"/usr/bin/python3 {fake_home}/antigona-status/server.py", "/"
    )
    assert validator._is_legacy_status(derived)

    old_literal = validator.ProcInfo(
        1, 1, 0, "/usr/bin/python3 /opt/antigona-home/antigona-status/server.py", "/"
    )
    assert not validator._is_legacy_status(old_literal)


# ── 7. read-only worker system prompt ────────────────────────────────────────


def test_readonly_prompt_workspace_is_derived() -> None:
    """The prompt's workspace line is the canonical resolver, not a literal path."""
    from antigona.turn_bridge.worker_adapter import (
        DEFAULT_WORKSPACE,
        READ_ONLY_SYSTEM_PROMPT,
    )

    assert DEFAULT_WORKSPACE == str(paths.project_root())
    assert f"Workspace is {DEFAULT_WORKSPACE}." in READ_ONLY_SYSTEM_PROMPT
