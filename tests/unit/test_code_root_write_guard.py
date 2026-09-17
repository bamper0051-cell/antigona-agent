"""Regression: the owner-class stores must never write into the code root.

The last write-class leftover was an out-of-unit **host-root CLI run** that
created ``elevation.db`` / ``audit_log.db`` inside the immutable code root,
because with ``HOME=/opt/antigona-home`` the owner-level state dir (``~/.antigona``)
collapsed onto the installed code checkout and the runtime resolvers fell back
to their dev default.  These tests lock the guard in:

* when ``owner_dir()`` *is* an Antigona source checkout, every owner-class
  runtime resolver fails closed (actionable ``RuntimeError``) instead of
  writing into the code root;
* a host-root CLI run performs ZERO writes into the code root (subprocess);
* with a governed state root the same stores work correctly and persist;
* the intentional dev/test default is only for a real owner-level state dir.

See ``tests/unit/test_runtime_state_root.py`` for the developer/deployment
default matrix.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from antigona.core import paths

#: Owner-class runtime stores: elevation / audit / traces / CLI state / MCP
#: registry / ownership ledgers / owner PIN.  These are the ones a host-root
#: CLI owner-mode run touches.
OWNER_CLASS_RESOLVERS = {
    "traces_db": paths.traces_db,
    "traces_log": paths.traces_log,
    "cli_state": paths.cli_state_file,
    "mcp_registry": paths.mcp_registry_file,
    "audit_log_db": paths.audit_log_db,
    "elevation_db": paths.elevation_db,
    "owner_pin_file": paths.owner_pin_file,
    "ownership_db_dir": paths.ownership_db_dir,
    "cli_aliases": paths.cli_aliases_file,
    "cli_theme": paths.cli_theme_file,
    "cli_user_themes": paths.cli_user_themes_file,
}

_OWNER_CLASS_ENVS = (
    "ANTIGONA_STATE_ROOT",
    "ANTIGONA_IMMUTABLE_DEPLOYMENT",
    "ANTIGONA_TRACES_DB",
    "ANTIGONA_TRACES_LOG",
    "ANTIGONA_STATE_FILE",
    "ANTIGONA_MCP_REGISTRY_FILE",
    "ANTIGONA_AUDIT_DB_PATH",
    "ANTIGONA_ELEVATION_DB_PATH",
    "ANTIGONA_OWNER_PIN_FILE",
    "ANTIGONA_OWNERSHIP_DIR",
    "ANTIGONA_CLI_ALIASES_FILE",
    "ANTIGONA_CLI_THEME_FILE",
    "ANTIGONA_CLI_USER_THEMES_FILE",
)


def _make_installed_code_root(tmp_path: Path) -> Path:
    """Create a synthetic installed code checkout at ``<home>/.antigona``.

    Mirrors the hardened install: ``HOME`` points at the parent, so
    ``owner_dir()`` collapses onto the code root exactly like a host-root run.
    """
    home = tmp_path / "root"
    code_root = home / ".antigona"
    (code_root / "src" / "antigona").mkdir(parents=True)
    (code_root / "pyproject.toml").write_text(
        "[project]\nname = \"antigona\"\n", encoding="utf-8"
    )
    (code_root / "src" / "antigona" / "__init__.py").write_text("", encoding="utf-8")
    return home


def test_owner_dir_is_code_root_detection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = _make_installed_code_root(tmp_path)
    monkeypatch.setenv("HOME", str(home))
    assert paths.owner_dir() == home / ".antigona"
    assert paths.owner_dir_is_code_root() is True

    # A genuine owner-level state directory is not a code checkout.
    plain_home = tmp_path / "plain"
    (plain_home / ".antigona").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(plain_home))
    assert paths.owner_dir_is_code_root() is False


def test_owner_class_stores_fail_closed_when_home_is_code_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No state root + HOME on the code root => actionable fail-closed."""
    home = _make_installed_code_root(tmp_path)
    for name in _OWNER_CLASS_ENVS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(home))

    for name, resolver in OWNER_CLASS_RESOLVERS.items():
        with pytest.raises(RuntimeError) as excinfo:
            resolver()  # type: ignore[operator]
        assert "ANTIGONA_STATE_ROOT" in str(excinfo.value), (
            f"{name} fail-closed error is not actionable: {excinfo.value}"
        )


def test_owner_class_stores_allowed_with_state_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A governed state root wins even when HOME is on the code root."""
    home = _make_installed_code_root(tmp_path)
    state_root = tmp_path / "var-lib-antigona"
    for name in _OWNER_CLASS_ENVS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ANTIGONA_STATE_ROOT", str(state_root))

    for name, resolver in OWNER_CLASS_RESOLVERS.items():
        resolved = Path(resolver())  # type: ignore[operator]
        assert str(resolved).startswith(str(state_root)), (
            f"{name} ignored the state root: {resolved}"
        )
        assert not str(resolved).startswith(str(home / ".antigona")), (
            f"{name} resolved under the code root: {resolved}"
        )


def test_state_rooted_elevation_and_audit_stores_work(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The elevation and audit stores are fully functional under the state root."""
    home = _make_installed_code_root(tmp_path)
    state_root = tmp_path / "var-lib-antigona"
    for name in _OWNER_CLASS_ENVS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ANTIGONA_STATE_ROOT", str(state_root))

    from antigona.security.audit import SystemAuditLogger
    from antigona.security.elevation import CLI_OWNER_PRINCIPAL, owner_elevation_authority

    elevation = owner_elevation_authority()
    assert Path(elevation.db_path) == state_root / "elevation.db"
    elevation.elevate(CLI_OWNER_PRINCIPAL)
    assert owner_elevation_authority().is_elevated(CLI_OWNER_PRINCIPAL) is True

    audit = SystemAuditLogger()
    assert Path(audit._db_path) == state_root / "audit_log.db"
    audit.log_action(
        channel="cli",
        user_id="owner",
        session_id="test-session",
        command="TOOL:noop",
        exit_code=0,
    )
    assert (state_root / "elevation.db").is_file()
    assert (state_root / "audit_log.db").is_file()

    # Nothing leaked back into the code root.
    code_root = home / ".antigona"
    leaked = [
        p for p in code_root.rglob("*")
        if p.is_file() and p.name in {"elevation.db", "audit_log.db", "owner_pin.json"}
    ]
    assert leaked == [], f"owner-class store leaked into the code root: {leaked}"


def test_host_root_cli_run_writes_nothing_into_code_root(tmp_path: Path) -> None:
    """A real host-root CLI store resolution performs zero code-root writes.

    Runs the exact owner-class code paths the interactive CLI uses (the shared
    elevation authority and the system audit logger) as a subprocess with
    ``HOME`` on a synthetic installed code root and no governed state root.
    The run must fail closed and must NOT create a single file in the code root.
    """
    home = _make_installed_code_root(tmp_path)
    code_root = home / ".antigona"
    before = {p for p in code_root.rglob("*")}

    repo_root = Path(__file__).resolve().parents[2]
    env = dict(os.environ)
    for name in _OWNER_CLASS_ENVS:
        env.pop(name, None)
    env["HOME"] = str(home)
    env["ANTIGONA_PROJECT_ROOT"] = str(repo_root)
    # Run against the repo's own src tree, exactly like a host-root CLI whose
    # PYTHONPATH points at the installed code root.
    env["PYTHONPATH"] = str(repo_root / "src")
    env.pop("PYTHONDONTWRITEBYTECODE", None)

    program = (
        "from antigona.security.elevation import owner_elevation_authority;"
        "from antigona.security.audit import SystemAuditLogger;"
        "owner_elevation_authority();"
        "SystemAuditLogger();"
        "print('UNEXPECTED: stores resolved into the code root')"
    )
    proc = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(repo_root),
    )
    assert proc.returncode != 0, (
        "host-root CLI store resolution unexpectedly succeeded: "
        f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )
    assert "ANTIGONA_STATE_ROOT" in proc.stderr, (
        f"fail-closed error is not actionable: {proc.stderr!r}"
    )

    after = {p for p in code_root.rglob("*")}
    created = sorted(str(p.relative_to(code_root)) for p in (after - before) if p.is_file())
    assert created == [], f"host-root CLI run wrote into the code root: {created}"
