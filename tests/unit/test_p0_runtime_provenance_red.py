"""Narrow RED regression test for P0 session DB provenance & validator contract."""

from pathlib import Path
from typing import Any

from antigona.core import paths
from antigona.sessions.database import DEFAULT_DB_PATH, SessionDatabase, _resolve_db_path
from antigona.startup import validator


def test_session_db_provenance_uses_canonical_paths(monkeypatch: Any) -> None:
    """Session database must derive default path from antigona.core.paths.sessions_db_path()."""
    # 1. Test DEFAULT_DB_PATH matches canonical sessions_db_path()
    canonical_expected = str(paths.sessions_db_path().resolve())
    assert DEFAULT_DB_PATH == canonical_expected, (
        f"DEFAULT_DB_PATH ({DEFAULT_DB_PATH}) does not match canonical sessions_db_path ({canonical_expected})"
    )

    # 2. Test when ANTIGONA_PROJECT_ROOT is overridden, _resolve_db_path(None) follows canonical root
    custom_root = Path("/tmp/test_canonical_root")
    monkeypatch.delenv("ANTIGONA_STATE_ROOT", raising=False)
    monkeypatch.setenv("ANTIGONA_PROJECT_ROOT", str(custom_root))
    expected_path = str((custom_root / "antigona_sessions.db").resolve())

    resolved = _resolve_db_path(None)
    assert resolved == expected_path, (
        f"_resolve_db_path(None) returned '{resolved}', expected '{expected_path}'"
    )

    db = SessionDatabase()
    assert db.db_path == expected_path, (
        f"SessionDatabase().db_path returned '{db.db_path}', expected '{expected_path}'"
    )


def test_session_db_provenance_respects_env_override(monkeypatch: Any) -> None:
    """ANTIGONA_SESSION_DB_PATH override must be respected by SessionDatabase."""
    override_db = Path("/tmp/custom_sessions_test.db")
    monkeypatch.setenv("ANTIGONA_SESSION_DB_PATH", str(override_db))

    resolved = _resolve_db_path(None)
    assert resolved == str(override_db.resolve()), (
        f"_resolve_db_path(None) returned '{resolved}', expected '{override_db.resolve()}'"
    )


def test_validator_c6_detects_foreign_db(monkeypatch: Any) -> None:
    """Validator contract C6 must mark foreign worktree DB as unexpected and fail closed."""
    # Simulate processes holding open DB fds
    procs = [
        validator.ProcInfo(
            pid=9999,
            ppid=1,
            start=1000.0,
            cmdline=".venv/bin/python -m antigona.gateway",
            cwd=str(paths.project_root()),
        )
    ]
    # Mock open_db_files to return foreign worktree DB
    foreign_db = "/opt/antigona-home/antigona-wt-integrate-nis94d/antigona_sessions.db"
    monkeypatch.setattr(validator, "open_db_files", lambda p: {foreign_db, str(paths.database_path())})

    monkeypatch.setenv("ANTIGONA_OWNER_ID", "123")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("ANTIGONA_PIN", "123456")

    results = validator.check_post(procs)
    c6 = next(r for r in results if r.check == "contract:C6:db")
    assert not c6.ok, "C6 must fail when foreign DB file is open"
    assert foreign_db in c6.detail


def test_validator_main_fails_closed_on_exception(monkeypatch: Any) -> None:
    """Validator main() must fail closed (return exit code 1) on unhandled exception."""
    monkeypatch.delenv("ANTIGONA_SKIP_VALIDATOR", raising=False)
    monkeypatch.setattr("sys.argv", ["validator", "--check=post"])
    monkeypatch.setattr(validator, "run", lambda check: 1 / 0)  # raise ZeroDivisionError

    code = validator.main()
    assert code == 1, f"validator.main() returned {code}, expected 1 (fail-closed)"


def test_validator_main_fails_closed_on_critical_check(monkeypatch: Any) -> None:
    """Validator main() must return exit code 1 when check_post returns CRITICAL failure."""
    monkeypatch.delenv("ANTIGONA_SKIP_VALIDATOR", raising=False)
    monkeypatch.setattr(validator, "_all_procs", lambda: [])
    monkeypatch.setattr(
        validator,
        "check_post",
        lambda procs: [
            validator.CheckResult(
                check="contract:C6:db",
                severity="CRITICAL",
                ok=False,
                detail="test failure",
            )
        ],
    )
    monkeypatch.setattr("sys.argv", ["validator", "--check=post"])

    code = validator.main()
    assert code == 1, f"validator.main() returned {code}, expected 1 for CRITICAL check failure"

