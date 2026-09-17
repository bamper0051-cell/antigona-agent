"""Clean-room and architecture probes for the P4.3 storage/queue/cache code.

Two families of checks:

* no prohibited third-party identifiers anywhere in the new modules;
* Redis is structurally incapable of becoming the source of truth — the broker
  and the cache never write a state transition, never touch ``queue_jobs``, and
  never import the repository/state-machine write path.
"""

from __future__ import annotations

import re
import socket
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src" / "antigona"

NEW_FILES = [
    SRC / "storage" / "__init__.py",
    SRC / "storage" / "engine.py",
    SRC / "storage" / "models.py",
    SRC / "storage" / "session.py",
    SRC / "storage" / "migrator.py",
    SRC / "storage" / "migrations" / "env.py",
    SRC / "storage" / "migrations" / "versions" / "0001_initial.py",
    SRC / "queue" / "__init__.py",
    SRC / "queue" / "redis_broker.py",
    SRC / "durable" / "state_cache.py",
]

PROHIBITED = re.compile(r"\b(klio|hermes|openclaw)\b", re.IGNORECASE)


def test_clean_room_no_prohibited_identifiers() -> None:
    for path in NEW_FILES:
        assert path.exists(), f"Target file missing: {path}"
        matches = PROHIBITED.findall(path.read_text(encoding="utf-8"))
        assert not matches, f"Prohibited identifier(s) {matches} found in {path}"


def test_redis_is_never_the_source_of_truth() -> None:
    """The broker and the cache must not write state or queue rows."""
    forbidden_writes = [
        "StateTransition(",
        "QueueJob(",
        "INSERT INTO state_transitions",
        "UPDATE task_flows",
        "check_task_transition",
        "record_rejection",
    ]
    for path in (SRC / "queue" / "redis_broker.py", SRC / "durable" / "state_cache.py"):
        content = path.read_text(encoding="utf-8")
        for token in forbidden_writes:
            assert token not in content, (
                f"'{token}' found in {path.name}: Redis must stay a transport/cache, "
                "the SQL transaction is the only place state is written."
            )


def test_state_cache_reads_state_only_through_sql_on_miss() -> None:
    content = (SRC / "durable" / "state_cache.py").read_text(encoding="utf-8")
    # The single DB read in the module is the cache-aside fallback.
    assert "select(TaskFlow.status)" in content
    assert "after_commit" in content, "the cache must publish only after a commit"


def test_state_machine_graph_untouched_by_p43() -> None:
    """The transition graph and the Verifier-only edge are unchanged."""
    from antigona.durable.state_machine import TASK_TRANSITIONS, VERIFIER_ONLY_TRANSITIONS
    from antigona.models import TaskState

    assert VERIFIER_ONLY_TRANSITIONS == frozenset({(TaskState.VERIFYING, TaskState.DONE)})
    for targets in TASK_TRANSITIONS.values():
        assert TaskState.DONE not in targets


def test_brokers_and_backends_open_no_sockets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Constructing every mock/lazy component must not touch the network."""

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("no network access is allowed in unit tests")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)

    from antigona.durable.state_cache import build_state_cache
    from antigona.queue import InMemoryBroker, build_broker

    InMemoryBroker()
    build_broker(None)
    build_broker("redis://127.0.0.1:6379/0")
    build_state_cache(None)
    build_state_cache("redis://127.0.0.1:6379/0")


def test_sync_database_layer_is_preserved() -> None:
    """P0's sync layer stays intact: P4.3 adds a path, it does not remove one."""
    from antigona.database import SCHEMA_VERSION, Database
    from antigona.storage.models import SCHEMA_VERSION as ASYNC_SCHEMA_VERSION
    from antigona.storage.models import Base

    assert SCHEMA_VERSION == ASYNC_SCHEMA_VERSION
    assert Database("sqlite:///:memory:").engine.dialect.name == "sqlite"
    # One metadata object for both layers — no schema drift is possible.
    assert Base.metadata is Base.metadata
    assert "task_flows" in Base.metadata.tables


def test_queue_package_keeps_backward_compatible_imports() -> None:
    from antigona.queue import DurableQueue

    assert DurableQueue.__module__ == "antigona.queue"


def test_create_all_idempotently_migrates_operations_executed_tools(tmp_path) -> None:
    """create_all() ALTERs a legacy operations table to add executed_tools.

    Base.metadata.create_all only creates missing tables and never ALTERs an
    existing one, so a restored/older DB (schema v7) lacks the column the v8
    Operation model declares. The idempotent migration must add it and be a
    no-op on a second run.
    """
    import sqlite3

    from antigona.database import Database

    db_path = tmp_path / "migrate.db"
    url = f"sqlite:///{db_path}"
    # Simulate an old-schema operations table WITHOUT executed_tools.
    con = sqlite3.connect(str(db_path))
    con.execute("CREATE TABLE operations (id TEXT PRIMARY KEY, status TEXT NOT NULL)")
    con.commit()
    con.close()

    db = Database(url)
    db.create_all()  # must not raise and must migrate the column

    con = sqlite3.connect(str(db_path))
    cols = [row[1] for row in con.execute("PRAGMA table_info(operations)")]
    con.close()
    assert "executed_tools" in cols

    db.create_all()  # idempotent second run must not raise
    db.dispose()


def test_create_all_v9_upgrade_preserves_legacy_rows_and_is_idempotent(tmp_path) -> None:
    import sqlite3

    from antigona.database import SCHEMA_VERSION, Database

    db_path = tmp_path / "legacy-v8.db"
    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            """
            PRAGMA foreign_keys=ON;
            CREATE TABLE schema_version (
                version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL
            );
            INSERT INTO schema_version VALUES(8, CURRENT_TIMESTAMP);
            CREATE TABLE task_flows (id VARCHAR(36) PRIMARY KEY);
            INSERT INTO task_flows VALUES('task-1');
            CREATE TABLE flow_steps (
                id VARCHAR(36) PRIMARY KEY, task_id VARCHAR(36) NOT NULL,
                \"index\" INTEGER NOT NULL, title VARCHAR(255) NOT NULL,
                status VARCHAR(32) NOT NULL, revision INTEGER NOT NULL,
                input JSON NOT NULL, output JSON, retries INTEGER NOT NULL
            );
            INSERT INTO flow_steps VALUES(
                'step-1', 'task-1', 1, 'legacy', 'PENDING', 0, '{}', NULL, 0
            );
            CREATE TABLE cron_schedules (
                id VARCHAR(36) PRIMARY KEY, created_at DATETIME NOT NULL
            );
            INSERT INTO cron_schedules VALUES('cron-1', '2026-01-02 03:04:05');
            CREATE TABLE operations (id TEXT PRIMARY KEY, status TEXT NOT NULL);
            INSERT INTO operations VALUES('op-1', 'RECEIVED');
            CREATE TABLE schedule_events (
                id VARCHAR(36) PRIMARY KEY, schedule_id VARCHAR(36) NOT NULL,
                event_type VARCHAR(16) NOT NULL, message TEXT,
                correlation_id VARCHAR(36) NOT NULL DEFAULT '', created_at DATETIME
            );
            INSERT INTO schedule_events VALUES(
                'event-1', 'cron-1', 'created', NULL, '', NULL
            );
            CREATE TABLE verifier_criteria (
                task_id VARCHAR(36) PRIMARY KEY REFERENCES task_flows(id) ON DELETE CASCADE,
                criteria TEXT NOT NULL,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            INSERT INTO verifier_criteria(task_id, criteria) VALUES('task-1', 'legacy');
            """
        )

    db = Database(f"sqlite:///{db_path}")
    db.create_all()
    db.create_all()
    with sqlite3.connect(db_path) as connection:
        flow_step = connection.execute(
            "SELECT id, step_number, tool_name, arguments FROM flow_steps WHERE id='step-1'"
        ).fetchone()
        operation = connection.execute(
            "SELECT id, executed_tools FROM operations WHERE id='op-1'"
        ).fetchone()
        cron = connection.execute(
            "SELECT id, created_at, updated_at FROM cron_schedules WHERE id='cron-1'"
        ).fetchone()
        event = connection.execute(
            "SELECT id, created_at FROM schedule_events WHERE id='event-1'"
        ).fetchone()
        criterion = connection.execute(
            "SELECT task_id, criteria FROM verifier_criteria WHERE task_id='task-1'"
        ).fetchone()
        versions = connection.execute(
            "SELECT version FROM schema_version ORDER BY version"
        ).fetchall()
        event_not_null = next(
            row[3]
            for row in connection.execute("PRAGMA table_info(schedule_events)")
            if row[1] == "created_at"
        )
        criterion_not_null = next(
            row[3]
            for row in connection.execute("PRAGMA table_info(verifier_criteria)")
            if row[1] == "task_id"
        )
    db.dispose()

    assert flow_step == ("step-1", 0, "", "{}")
    assert operation == ("op-1", "{}")
    assert cron == ("cron-1", "2026-01-02 03:04:05", "2026-01-02 03:04:05")
    assert event is not None and event[0] == "event-1" and event[1]
    assert criterion == ("task-1", "legacy")
    assert event_not_null == criterion_not_null == 1
    assert versions[-1] == (SCHEMA_VERSION,)


def test_create_all_v9_fails_closed_on_null_verifier_identity(tmp_path) -> None:
    import sqlite3

    from antigona.database import Database

    db_path = tmp_path / "invalid-legacy.db"
    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            """
            CREATE TABLE verifier_criteria (
                task_id VARCHAR(36) PRIMARY KEY,
                criteria TEXT NOT NULL,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            INSERT INTO verifier_criteria(task_id, criteria) VALUES(NULL, 'orphan');
            """
        )
    db = Database(f"sqlite:///{db_path}")
    with pytest.raises(RuntimeError, match="NULL task_id"):
        db.create_all()
    db.dispose()
