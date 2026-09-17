"""Regression coverage for policy isolation and approval-resume ordering."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APPROVAL = "tests/unit/test_failure_b_approval_resume.py"
POLICY = "tests/unit/test_policy_workspace_write_auto.py"

def _run_order(*paths: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.pop("PYTEST_CURRENT_TEST", None)
    return subprocess.run([sys.executable, "-m", "pytest", *paths, "-q", "--tb=short"], cwd=ROOT, env=env, capture_output=True, text=True, check=False)

def test_approval_resume_survives_policy_test_before_it() -> None:
    result = _run_order(POLICY, APPROVAL)
    assert result.returncode == 0, result.stdout + result.stderr

def test_approval_resume_survives_policy_test_after_it() -> None:
    result = _run_order(APPROVAL, POLICY)
    assert result.returncode == 0, result.stdout + result.stderr
