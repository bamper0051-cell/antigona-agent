"""Regression guard for B58: no TRACKED path may be matched by a .gitignore rule.

Independent review (P27/M27) found that the new runtime-state ignore block added
to ``.gitignore`` contained a literal rule (``.antigona/gateway_config.json``)
that matched a path which is *already tracked* in the index. Git never applies
ignore rules to tracked files, so the rule protected nothing while its comment
claimed the tracked ``.antigona/`` content "stays tracked" -- the comment
contradicted the observed fact:

    git ls-files -ci --exclude-standard   ->  .antigona/gateway_config.json
    git check-ignore -v --no-index .antigona/gateway_config.json
                                          ->  .gitignore:51

These tests pin the invariant directly through git: no tracked path may be
reported as ignored. They name the offending path(s) in the failure message so a
future regression is self-diagnosing. The git context is discovered via
subprocess (``git rev-parse --show-toplevel``) -- no hardcoded repository path --
and the tests skip cleanly when run outside a git work tree.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest


def _run_git(repo_root: str, *args: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    """Run a git subcommand inside ``repo_root`` and capture text output."""

    return subprocess.run(
        ["git", "-C", repo_root, *args],
        capture_output=True,
        input=stdin,
        text=True,
        check=False,
    )


@pytest.fixture(scope="module")
def repo_root() -> str:
    """Return the enclosing git work-tree root, or skip when not in a repo."""

    if shutil.which("git") is None:
        pytest.skip("git executable not available")
    probe = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=False,
    )
    root = probe.stdout.strip()
    if probe.returncode != 0 or not root:
        pytest.skip("not inside a git work tree")
    return root


def test_no_tracked_path_matches_an_ignore_rule(repo_root: str) -> None:
    """``git ls-files -ci --exclude-standard`` must never list a tracked path."""

    result = _run_git(repo_root, "ls-files", "-ci", "--exclude-standard")
    assert result.returncode == 0, (
        f"git ls-files -ci --exclude-standard failed (rc={result.returncode}):\n{result.stderr}"
    )

    offenders = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    assert offenders == [], (
        "B58 regression: tracked path(s) are matched by .gitignore rules, but git "
        "never ignores tracked files -- the rule protects nothing and its comment "
        "misstates reality. Offenders: "
        f"{offenders}. Fix .gitignore so the rule does not match a tracked path "
        "(remove the literal path, or negate it with '!' when a glob is involved)."
    )


def test_check_ignore_reports_no_tracked_path(repo_root: str) -> None:
    """``git check-ignore --no-index`` over every tracked file must match nothing."""

    tracked = _run_git(repo_root, "ls-files")
    assert tracked.returncode == 0, f"git ls-files failed (rc={tracked.returncode}):\n{tracked.stderr}"

    checked = _run_git(repo_root, "check-ignore", "--stdin", "--no-index", stdin=tracked.stdout)
    # git check-ignore exits 0 when at least one path matched, 1 when none did.
    assert checked.returncode in (0, 1), (
        f"git check-ignore --no-index failed (rc={checked.returncode}):\n{checked.stderr}"
    )

    offenders = [line for line in checked.stdout.splitlines() if line.strip()]
    assert offenders == [], (
        "B58 regression: these TRACKED paths match .gitignore rules (offender -> "
        f"rule): {offenders}. A tracked path must stay tracked; adjust the rule so "
        "it does not match an indexed path."
    )
