"""L0-01 / L0-02 / L0-04: real Antigona CLI smoke (help, version, invalid)."""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src"
CANDIDATE_PYTHON = REPO_ROOT / ".venv" / "bin" / "python"
CLI = [str(CANDIDATE_PYTHON if CANDIDATE_PYTHON.is_file() else sys.executable), "-m", "antigona.cli"]
SEMVER = re.compile(r"\b\d+\.\d+\.\d+\b")
TRACEBACK = "Traceback (most recent call last)"


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    # L0 must be independent of the invoking shell, live installs, and repo state.
    with tempfile.TemporaryDirectory(prefix="antigona-l0-") as runtime:
        runtime_path = Path(runtime)
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)
        env.update(
            {
                "ANTIGONA_DATABASE_URL": f"sqlite+aiosqlite:///{runtime_path / 'runtime.db'}",
                "ANTIGONA_SESSION_DB_PATH": str(runtime_path / "sessions.db"),
                "ANTIGONA_SECURITY_DIR": str(runtime_path / "security"),
                "ANTIGONA_OWNERSHIP_DIR": str(runtime_path / "ownership"),
            }
        )
        if not CANDIDATE_PYTHON.is_file():
            env["PYTHONPATH"] = str(SRC_ROOT)
        return subprocess.run(
            [*CLI, *args], cwd=runtime_path, env=env, capture_output=True, text=True
        )


def _combined(proc: subprocess.CompletedProcess[str]) -> str:
    return f"{proc.stdout}\n{proc.stderr}"


def test_l0_01_help_twice() -> None:
    runs = [_run(["--help"]) for _ in range(2)]
    for r in runs:
        text = _combined(r)
        assert r.returncode == 0
        assert "Usage" in text
        assert TRACEBACK not in text
    assert runs[0].returncode == runs[1].returncode
    assert ("Usage" in _combined(runs[0])) == ("Usage" in _combined(runs[1]))


def test_l0_02_version_twice() -> None:
    runs = [_run(["--version"]) for _ in range(2)]
    for r in runs:
        text = _combined(r)
        assert r.returncode == 0
        assert SEMVER.search(text), text
        assert TRACEBACK not in text
    assert runs[0].returncode == runs[1].returncode
    assert bool(SEMVER.search(_combined(runs[0]))) == bool(SEMVER.search(_combined(runs[1])))


def test_l0_04_invalid_command_twice() -> None:
    runs = [_run(["invalid_cmd_xyz"]) for _ in range(2)]
    for r in runs:
        text = _combined(r)
        assert r.returncode != 0
        assert TRACEBACK not in text
        assert "No such command" in text or "invalid" in text.lower()
    assert runs[0].returncode == runs[1].returncode
    assert (TRACEBACK in _combined(runs[0])) == (TRACEBACK in _combined(runs[1]))
