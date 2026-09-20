"""Architecture enforcement tests: Unified Paths API, Runtime Validator, Arch Guard."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from antigona.core import paths
from antigona.startup import validator

# ─── Unified Paths API ────────────────────────────────────────────────────────


def test_paths_canonical_defaults(monkeypatch, tmp_path):
    # The dev/test default applies only with no governed runtime root and no
    # immutable marker (see tests/unit/test_runtime_state_root.py for the
    # hardened-deployment contract).
    # Isolate HOME so owner_dir() is a genuine state directory, not the code
    # checkout a host-root run would collapse onto (that case fails closed).
    home = tmp_path / "home"
    (home / ".antigona").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    for name in ("ANTIGONA_PROJECT_ROOT", "ANTIGONA_DATABASE_URL",
                 "ANTIGONA_WORKSPACE", "ANTIGONA_STATE_ROOT",
                 "ANTIGONA_IMMUTABLE_DEPLOYMENT", "ANTIGONA_TELEGRAM_TURN_LEDGER",
                 "ANTIGONA_SESSION_DB_PATH", "ANTIGONA_TASKS_DIR",
                 "ANTIGONA_HEALTH_DIR"):
        monkeypatch.delenv(name, raising=False)
    # Корень резолвится из расположения пакета (editable src-layout), не
    # хардкодится. Ожидаем канонический корень репозитория.
    proj_root = paths.project_root()
    assert proj_root.is_absolute()
    assert (proj_root / "pyproject.toml").exists(), "project root must contain pyproject.toml"
    owner_dir = Path.home() / ".antigona"
    assert str(paths.owner_dir()) == str(owner_dir)
    assert str(paths.project_local_dir()) == str(proj_root / ".antigona")
    assert str(paths.database_path()) == str(proj_root / "antigona.db")
    assert str(paths.sessions_db_path()) == str(proj_root / "antigona_sessions.db")
    assert str(paths.gateway_config_path()) == str(proj_root / ".antigona" / "gateway_config.json")
    assert str(paths.cli_state_file()) == str(owner_dir / "cli_state.json")
    assert str(paths.tasks_state_file()) == str(proj_root / ".tasks" / "tasks.json")


def test_paths_project_root_override(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTIGONA_PROJECT_ROOT", str(tmp_path))
    assert str(paths.project_root()) == str(tmp_path)
    assert str(paths.project_local_dir()) == str(tmp_path / ".antigona")


def test_paths_db_relative_url(monkeypatch):
    monkeypatch.setenv("ANTIGONA_DATABASE_URL", "sqlite:///./custom.db")
    assert str(paths.database_path()) == str(paths.project_root() / "custom.db")


# ─── Runtime Validator ────────────────────────────────────────────────────────


def _mk(pid, cmdline, ppid=0, cwd: str | None = None, start=1000.0) -> validator.ProcInfo:
    if cwd is None:
        cwd = str(paths.project_root())
    return validator.ProcInfo(pid=pid, ppid=ppid, start=start, cmdline=cmdline, cwd=cwd)


SVC_CMDLINES = {
    "gateway": ".venv/bin/python -m antigona.gateway ",
    "verifier": ".venv/bin/python -m antigona.verifier_service ",
    "worker": ".venv/bin/python -c from antigona.worker import main; main() ",
    "delivery": ".venv/bin/python -c from antigona.delivery_worker import main; main() ",
    "bot": ".venv/bin/python -c from antigona.channels.telegram.bot import main; main() ",
}


def test_validator_classify_single_each():
    procs = [_mk(i + 1000, cmd) for i, cmd in enumerate(SVC_CMDLINES.values())]
    counts = validator.classify(procs)
    assert counts == {"gateway": [1000], "verifier": [1001],
                      "worker": [1002], "delivery_worker": [1003],
                      "telegram_bot": [1004]}


def test_validator_classify_does_not_confuse_delivery_with_worker():
    procs = [_mk(1003, SVC_CMDLINES["delivery"])]
    counts = validator.classify(procs)
    assert counts["delivery_worker"] == [1003]
    assert counts["worker"] == []  # delivery_worker НЕ считается worker


def test_validator_pre_detects_stale(monkeypatch):
    procs = [_mk(1000, SVC_CMDLINES["bot"])]
    res = validator.check_pre(procs)
    assert not res[0].ok  # устаревший процесс стека -> не OK
    assert validator.check_pre([])[0].ok  # пусто -> OK


def test_validator_post_contracts(monkeypatch):
    monkeypatch.setenv("ANTIGONA_OWNER_ID", "123")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("ANTIGONA_PIN", "123456")
    monkeypatch.setattr(validator, "check_worktree_integrity", lambda: validator.CheckResult("C11", "CRITICAL", True, "test manifest isolated"))
    procs = [_mk(i + 1000, cmd) for i, cmd in enumerate(SVC_CMDLINES.values())]
    results = validator.check_post(procs)
    assert all(r.ok for r in results), [(r.check, r.detail) for r in results if not r.ok]


def test_validator_post_missing_token(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("ANTIGONA_OWNER_ID", "123")
    monkeypatch.setenv("ANTIGONA_PIN", "123456")
    procs = [_mk(i + 1000, cmd) for i, cmd in enumerate(SVC_CMDLINES.values())]
    results = validator.check_post(procs)
    token = next(r for r in results if r.check == "contract:C9:bot_token")
    assert not token.ok


# ─── Architecture Guard (CI enforcement) ──────────────────────────────────────


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def test_arch_guard_passes_with_baseline():
    root = _repo_root()
    r = subprocess.run(
        [sys.executable, str(root / "scripts" / "arch_guard.py"),
         "--baseline", str(root / "scripts" / "arch_baseline.txt")],
        cwd=str(root), capture_output=True, text=True,
    )
    assert r.returncode == 0, f"arch_guard failed:\n{r.stdout}\n{r.stderr}"


def _make_guard_sandbox(dest: Path) -> Path:
    """Minimal out-of-tree copy of the tree ``arch_guard.py`` scans (F-20260918T2118Z)."""
    root = _repo_root()
    (dest / "scripts").mkdir(parents=True, exist_ok=True)
    shutil.copy2(root / "scripts" / "arch_guard.py", dest / "scripts" / "arch_guard.py")
    shutil.copy2(root / "scripts" / "arch_baseline.txt", dest / "scripts" / "arch_baseline.txt")
    shutil.copytree(
        root / "src" / "antigona",
        dest / "src" / "antigona",
        ignore=shutil.ignore_patterns("__pycache__"),
        dirs_exist_ok=True,
    )
    return dest


def test_arch_guard_fails_on_new_violation(tmp_path):
    """Добавление нового Path.home() вне core/paths.py -> guard exit != 0.

    Проба пишется в sandbox ВНЕ репозитория (F-20260918T2118Z): замороженное
    дерево не должно содержать посторонний deployment-файл ни на мгновение
    (C11 startup gate fail-closed на unknown deployment file).
    """
    root = _repo_root()
    sandbox = _make_guard_sandbox(tmp_path / "guard_sandbox")
    probe = sandbox / "src" / "antigona" / "_arch_probe_tmp.py"
    assert not probe.is_relative_to(root), "probe must never be written inside the repository"
    probe.write_text("from pathlib import Path\nBAD = Path.home()\n", encoding="utf-8")
    try:
        r = subprocess.run(
            [sys.executable, str(sandbox / "scripts" / "arch_guard.py"),
             "--baseline", str(sandbox / "scripts" / "arch_baseline.txt")],
            cwd=str(sandbox), capture_output=True, text=True,
        )
        assert r.returncode != 0
        assert "_arch_probe_tmp.py" in r.stdout
    finally:
        probe.unlink(missing_ok=True)
