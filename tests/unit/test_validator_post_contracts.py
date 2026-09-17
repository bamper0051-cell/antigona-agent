import sqlite3
from pathlib import Path

from antigona.core import paths
from antigona.startup import validator


def make_db(p, n=0):
    with sqlite3.connect(p) as c:
        c.executescript("CREATE TABLE telegram_turns (id INTEGER); CREATE TABLE telegram_queue (id INTEGER);")
        c.executemany("INSERT INTO telegram_turns VALUES (?)", [(i,) for i in range(n)])

def test_empty_task_db_healthy(tmp_path):
    p=tmp_path/"telegram_turns.db"; make_db(p)
    assert validator._task_db_state(str(p)) == "empty-healthy"

def test_active_task_db_stale(tmp_path):
    p=tmp_path/"telegram_turns.db"; make_db(p, 1)
    assert validator._task_db_state(str(p)) == "stale-active"

def test_orchestration_systemd_unit(monkeypatch):
    monkeypatch.setattr(Path, "read_text", lambda self: "0::/system.slice/antigona-orchestration.service")
    assert validator._systemd_unit(123) == "antigona-orchestration.service"


def test_unrelated_systemd_unit_is_not_an_ownership_exemption(monkeypatch):
    monkeypatch.setattr(Path, "read_text", lambda self: "0::/system.slice/unrelated.service")
    assert validator._systemd_unit(123) == "unrelated.service"
    assert validator._systemd_unit(123) not in validator.CANONICAL_ANTIGONA_UNITS

def test_malformed_cgroup_is_rejected(monkeypatch):
    monkeypatch.setattr(Path, "read_text", lambda self: "0::/system.slice/antigona-worker.service/../../unrelated.service")
    assert validator._systemd_unit(123) == ""

def test_unknown_ppid_one_process_is_not_exempt(monkeypatch):
    monkeypatch.setattr(Path, "read_text", lambda self: "0::/system.slice/unknown.service")
    assert validator._systemd_unit(123) not in validator.CANONICAL_ANTIGONA_UNITS

def test_legacy_status_requires_exact_command_and_cwd():
    legacy = f"/usr/bin/python3 {paths.home_dir()}/antigona-status/server.py"
    base = dict(pid=1, ppid=1, start=0, cwd="/")
    assert validator._is_legacy_status(validator.ProcInfo(cmdline=legacy, **base))
    assert not validator._is_legacy_status(validator.ProcInfo(cmdline=f"/tmp/wrapper {legacy}", **base))
    assert not validator._is_legacy_status(validator.ProcInfo(cmdline=f"{legacy} --json", **base))
    assert not validator._is_legacy_status(validator.ProcInfo(cmdline=legacy, **{**base, "cwd": "/tmp"}))

def test_unreadable_task_db_is_not_healthy(tmp_path):
    p = tmp_path / "telegram_turns.db"; p.write_bytes(b"not sqlite")
    assert validator._task_db_state(str(p)) == "unreadable"


def test_queued_task_db_is_stale(tmp_path):
    p = tmp_path / "telegram_turns.db"
    make_db(p)
    with sqlite3.connect(p) as c:
        c.execute("INSERT INTO telegram_queue VALUES (1)")
    assert validator._task_db_state(str(p)) == "stale-active"
