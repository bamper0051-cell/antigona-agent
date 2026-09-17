from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Index, UniqueConstraint, inspect
from verifier_fakes import deterministic_test_judge

from antigona.database import SCHEMA_VERSION, Base, Database
from antigona.delivery import FakeAdapter, ProgressEvent
from antigona.models import StepState, TaskState
from antigona.queue import DurableQueue
from antigona.repository import CreateTask, InvalidTransition, TaskRepository
from antigona.verifier.criteria import CriteriaBase
from antigona.verifier_service import create_verifier_app


def test_migration_baseline_creates_all_orm_tables(tmp_path: Path) -> None:
    target = tmp_path / "migration.db"
    for sql_file in sorted(Path("migrations").glob("*.sql")):
        with sqlite3.connect(target) as connection:
            connection.executescript(sql_file.read_text())

    actual = set(inspect(Database(f"sqlite:///{target}").engine).get_table_names())
    assert actual == set(Base.metadata.tables) | set(CriteriaBase.metadata.tables) | {
        "schema_version"
    }
    with sqlite3.connect(target) as connection:
        latest_version = connection.execute(
            "SELECT MAX(version) FROM schema_version"
        ).fetchone()
    assert latest_version == (SCHEMA_VERSION,)


def test_migration_baseline_covers_registered_runtime_models(tmp_path: Path) -> None:
    """The flat baseline must not depend on which shared-Base models import first."""
    import antigona.kernel.models  # noqa: F401
    import antigona.orchestration.models  # noqa: F401
    import antigona.rca.storage  # noqa: F401

    target = tmp_path / "migration-with-runtime-models.db"
    for sql_file in sorted(Path("migrations").glob("*.sql")):
        with sqlite3.connect(target) as connection:
            connection.executescript(sql_file.read_text())

    actual = set(inspect(Database(f"sqlite:///{target}").engine).get_table_names())
    assert actual == set(Base.metadata.tables) | set(CriteriaBase.metadata.tables) | {
        "schema_version"
    }


def test_flat_migration_schema_matches_authoritative_orm(tmp_path: Path) -> None:
    """Flat provisioning matches ORM structure, with explicit physical extensions."""
    target = tmp_path / "migration-schema-parity.db"
    for sql_file in sorted(Path("migrations").glob("*.sql")):
        with sqlite3.connect(target) as connection:
            connection.executescript(sql_file.read_text())

    inspector = inspect(Database(f"sqlite:///{target}").engine)
    metadata_tables = {**Base.metadata.tables, **CriteriaBase.metadata.tables}
    assert set(inspector.get_table_names()) == set(metadata_tables) | {"schema_version"}

    for name, table in metadata_tables.items():
        actual_columns = {column["name"]: column for column in inspector.get_columns(name)}
        expected_columns = {column.name: column for column in table.columns}
        assert set(actual_columns) == set(expected_columns), name
        assert {
            column_name: bool(column["nullable"])
            for column_name, column in actual_columns.items()
        } == {
            column_name: bool(column.nullable)
            for column_name, column in expected_columns.items()
        }, name
        assert tuple(inspector.get_pk_constraint(name)["constrained_columns"]) == tuple(
            column.name for column in table.primary_key.columns
        ), name

        actual_fks = {
            (
                tuple(fk["constrained_columns"]),
                str(fk["referred_table"]),
                tuple(fk["referred_columns"]),
            )
            for fk in inspector.get_foreign_keys(name)
        }
        expected_fks = {
            ((column.name,), fk.column.table.name, (fk.column.name,))
            for column in table.columns
            for fk in column.foreign_keys
        }
        # The verifier ORM deliberately has no TaskFlow import/relationship;
        # deployed databases retain the legacy database-level integrity FK.
        if name == "verifier_criteria":
            expected_fks.add((("task_id",), "task_flows", ("id",)))
        assert actual_fks == expected_fks, name

        actual_unique = {
            tuple(item["column_names"])
            for item in inspector.get_unique_constraints(name)
        } | {
            tuple(item["column_names"])
            for item in inspector.get_indexes(name)
            if item["unique"]
        }
        expected_unique = {
            tuple(column.name for column in constraint.columns)
            for constraint in table.constraints
            if isinstance(constraint, UniqueConstraint)
        } | {
            tuple(column.name for column in index.columns)
            for index in table.indexes
            if index.unique
        }
        assert actual_unique == expected_unique, name

        actual_indexes = {
            tuple(item["column_names"])
            for item in inspector.get_indexes(name)
            if not item["unique"]
        }
        expected_indexes = {
            tuple(column.name for column in index.columns)
            for index in table.indexes
            if isinstance(index, Index) and not index.unique
        }
        # Historical query-optimization index, intentionally stronger than
        # the ORM's per-column index declarations.
        if name == "operations":
            expected_indexes.add(("chat_id", "status"))
        assert actual_indexes == expected_indexes, name


def test_schema_v9_marker_is_replay_safe(tmp_path: Path) -> None:
    target = tmp_path / "migration-replay.db"
    for sql_file in sorted(Path("migrations").glob("*.sql")):
        with sqlite3.connect(target) as connection:
            connection.executescript(sql_file.read_text())
    correction = Path("migrations/0009_schema_parity.sql").read_text()
    with sqlite3.connect(target) as connection:
        connection.executescript(correction)
        connection.executescript(correction)
        count = connection.execute(
            "SELECT COUNT(*) FROM schema_version WHERE version = ?", (SCHEMA_VERSION,)
        ).fetchone()
    assert count == (1,)


def test_runtime_has_no_done_capability_and_verifier_auth_is_required(tmp_path:Path,monkeypatch:pytest.MonkeyPatch)->None:
    url=f"sqlite:///{tmp_path/'db'}"; db=Database(url); db.create_all(); workspace=tmp_path/"w"; workspace.mkdir(); monkeypatch.setenv("ANTIGONA_WORKSPACE",str(workspace))
    with db.session_factory() as session:
        repo=TaskRepository(session); task,_=repo.create(CreateTask("o","g","x","value","k"))
        for target in (TaskState.QUEUED,TaskState.PLANNING,TaskState.TOOL_EXECUTING,TaskState.OBSERVING,TaskState.VERIFYING): repo.transition(task,target,"fixture","runtime")
        with pytest.raises(InvalidTransition): repo.transition(task,TaskState.DONE,"forged","runtime")
    with TestClient(create_verifier_app(url,"separate-secret",deterministic_test_judge())) as client:
        assert client.post("/verify",json={"task_id":task.id,"correlation_id":"x"}).status_code==401
        assert client.post("/verify",headers={"Authorization":"Bearer forged"},json={"task_id":task.id,"correlation_id":"x"}).status_code==401


def test_queue_lease_recovery_and_step_state_machine(tmp_path:Path)->None:
    db=Database(f"sqlite:///{tmp_path/'db'}"); db.create_all()
    with db.session_factory() as session:
        repo=TaskRepository(session); task,_=repo.create(CreateTask("o","g","x","v","k")); queue=DurableQueue(session); queue.enqueue(task); job=queue.claim("w",1); assert job and job.status=="RUNNING"; queue.heartbeat(job,"w",2)
        step=task.steps[0]; repo.transition_step(task,step,StepState.RUNNING,"start","worker"); repo.transition_step(task,step,StepState.COMPLETED,"done","worker")
        with pytest.raises(InvalidTransition): repo.transition_step(task,step,StepState.RUNNING,"rewind","worker")


def test_fake_progress_adapter_is_hermetic()->None:
    adapter=FakeAdapter(); event=ProgressEvent("t","s","c",None,"RUNNING","working"); adapter.deliver(event,"idem")
    assert adapter.events==[(event,"idem")]
