"""Regression coverage for policy isolation and approval-resume ordering."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
APPROVAL = "tests/unit/test_failure_b_approval_resume.py"
POLICY = "tests/unit/test_policy_workspace_write_auto.py"

#: Hard boundary for one nested pytest run.  Without it a stalling child (a
#: hung plugin, an I/O-blocked test, a deadlocked subprocess of the child) waits
#: forever and burns the whole CI job (``ci.yml`` only has
#: ``timeout-minutes: 30`` and no traceback pointing at the culprit).
#: pytest-timeout is deliberately NOT a dependency, so the boundary is enforced
#: here, in the parent, where the failure message can name the command.
_TIMEOUT_SECONDS = 120

#: Ambient pytest variables a nested run must not inherit: pytest itself owns
#: them and re-exporting them to the child changes its collection/reporting.
_ENV_VARS_TO_DROP = (
    "PYTEST_CURRENT_TEST",
    "PYTEST_DISABLE_PLUGIN_AUTOLOAD",
    "PYTEST_ADDOPTS",
)


def _run_order(*paths: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    for name in _ENV_VARS_TO_DROP:
        env.pop(name, None)
    cmd = [sys.executable, "-m", "pytest", *paths, "-q", "--tb=short"]
    try:
        return subprocess.run(
            cmd,
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=_TIMEOUT_SECONDS,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = _as_text(exc.stdout)
        stderr = _as_text(exc.stderr)
        pytest.fail(
            f"nested pytest run did not finish within the {_TIMEOUT_SECONDS}s "
            f"boundary (subprocess.TimeoutExpired): {cmd!r} cwd={ROOT} "
            f"paths={paths!r}; captured output on timeout is partial by nature"
            f"\n--- child stdout (partial) ---\n{stdout}"
            f"\n--- child stderr (partial) ---\n{stderr}",
            pytrace=False,
        )


def _as_text(stream: str | bytes | None) -> str:
    """``TimeoutExpired`` may hand back bytes, ``None`` or a partial stream."""
    if stream is None:
        return "<none>"
    if isinstance(stream, bytes):
        return stream.decode("utf-8", "replace")
    return stream

def test_approval_resume_survives_policy_test_before_it() -> None:
    result = _run_order(POLICY, APPROVAL)
    assert result.returncode == 0, result.stdout + result.stderr

def test_approval_resume_survives_policy_test_after_it() -> None:
    result = _run_order(APPROVAL, POLICY)
    assert result.returncode == 0, result.stdout + result.stderr
