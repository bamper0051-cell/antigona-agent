from __future__ import annotations

import os
import time
from collections.abc import Generator
from importlib import import_module
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy import event as sqlalchemy_event
from sqlalchemy.engine import ExceptionContext
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .observability import event as log_event

#: Current schema revision, bumped by every migration in ``migrations/*.sql``.
#: 7 = runtime tables (``operations``, ``evidence_registry``, ``telegram_message_bindings``).
#: 8 = shared runtime tables and the first operations compatibility upgrade.
#: 9 = canonical flat/ORM parity plus replay-safe legacy SQLite upgrades.
#: M1/M2/RCA shared-Base tables and structured goal columns are in flat migration 0008;
#: existing databases remain covered by the replay-safe Alembic/runtime column upgrade.
#: 10 = durable one-shot approval grants (``approvals.grant_token``).
#: 11 = delivery_receipts table for durable crash-after-send idempotency (DELIV-01).
SCHEMA_VERSION = 11


class Base(DeclarativeBase):
    pass


def _register_shared_models() -> None:
    """Load every model declared on ``Base`` before inspecting or creating it."""
    for module in (
        "antigona.models",
        "antigona.durable.operation_models",
        "antigona.kernel.models",
        "antigona.orchestration.models",
        "antigona.rca.storage",
    ):
        import_module(module)


class Database:
    def __init__(self, url: str, *, slow_query_threshold_ms: float | None = None) -> None:
        _register_shared_models()
        connect_args = {"check_same_thread": False, "timeout": 30} if url.startswith("sqlite") else {}
        self.slow_query_threshold_ms = (
            float(os.getenv("ANTIGONA_DB_SLOW_QUERY_MS", "250"))
            if slow_query_threshold_ms is None
            else slow_query_threshold_ms
        )
        if self.slow_query_threshold_ms < 0:
            raise ValueError("slow_query_threshold_ms must be non-negative")
        self.engine = create_engine(url, connect_args=connect_args)
        self._install_instrumentation()
        if url.startswith("sqlite"):

            @sqlalchemy_event.listens_for(self.engine, "connect")
            def configure(dbapi_connection: object, _: object) -> None:
                cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.close()

        self.session_factory = sessionmaker(self.engine, expire_on_commit=False)

    def _install_instrumentation(self) -> None:
        @sqlalchemy_event.listens_for(self.engine, "before_cursor_execute")
        def before_cursor_execute(
            _conn: object,
            _cursor: object,
            _statement: str,
            _parameters: object,
            context: Any,
            _executemany: bool,
        ) -> None:
            context._antigona_query_started = time.monotonic()

        @sqlalchemy_event.listens_for(self.engine, "after_cursor_execute")
        def after_cursor_execute(
            _conn: object,
            _cursor: object,
            _statement: str,
            _parameters: object,
            context: Any,
            _executemany: bool,
        ) -> None:
            duration_ms = self._duration_ms(context)
            log_event(
                "database.query.completed",
                service="database",
                correlation_id=None,
                status="slow" if duration_ms >= self.slow_query_threshold_ms else "ok",
                duration_ms=duration_ms,
            )

        @sqlalchemy_event.listens_for(self.engine, "handle_error")
        def handle_error(exception_context: ExceptionContext) -> None:
            duration_ms = self._duration_ms(exception_context.execution_context)
            log_event(
                "database.query.failed",
                service="database",
                correlation_id=None,
                status="error",
                duration_ms=duration_ms,
                error_type=type(exception_context.original_exception).__name__,
            )

    @staticmethod
    def _duration_ms(context: object | None) -> float:
        started = getattr(context, "_antigona_query_started", time.monotonic())
        return round((time.monotonic() - started) * 1000, 3)

    def create_all(self) -> None:
        from .storage.engine import postgres_append_only_ddl, sqlite_append_only_ddl

        Base.metadata.create_all(self.engine)
        # verifier_criteria lives on the verifier-private CriteriaBase, but the
        # gateway write path (core/task_service) starts from this shared init.
        # Create it here so the table exists alongside every other table on a
        # normal startup; fail-closed handling for an actual write failure lives
        # in core/task_service.py. Lazy import: the verifier package is fully
        # loaded by the time create_all() runs at app startup.
        from .verifier.criteria import CriteriaBase
        CriteriaBase.metadata.create_all(self.engine)
        self._ensure_schema_v9()
        self._ensure_approval_grant_column()
        self._ensure_goal_autonomy_columns()
        with self.engine.begin() as connection:
            connection.exec_driver_sql(
                "CREATE TABLE IF NOT EXISTS schema_version "
                "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            if self.engine.dialect.name == "sqlite":
                connection.exec_driver_sql(
                    "INSERT OR IGNORE INTO schema_version(version, applied_at) "
                    f"VALUES ({SCHEMA_VERSION}, CURRENT_TIMESTAMP)"
                )
                append_only_ddl = sqlite_append_only_ddl()
            else:
                connection.exec_driver_sql(
                    "INSERT INTO schema_version(version, applied_at) "
                    f"VALUES ({SCHEMA_VERSION}, CURRENT_TIMESTAMP) ON CONFLICT DO NOTHING"
                )
                append_only_ddl = postgres_append_only_ddl()
            for statement in append_only_ddl:
                # ``text()`` rather than ``exec_driver_sql``: the Postgres trigger body
                # contains a literal ``%``, which psycopg2 would treat as a placeholder.
                try:
                    with connection.begin_nested():
                        connection.execute(text(statement))
                except Exception:
                    pass

    def _ensure_schema_v9(self) -> None:
        """Idempotently upgrade legacy SQLite tables to the v9 canonical schema.

        SQLite cannot tighten an existing nullable column in place.  The two
        affected tables are rebuilt transactionally: schedule timestamps are
        backfilled without dropping rows, while an impossible NULL verifier
        primary key fails closed instead of silently inventing an identity.
        """
        if self.engine.dialect.name != "sqlite":
            return
        with self.engine.begin() as connection:
            additions = {
                "operations": {"executed_tools": "JSON NOT NULL DEFAULT '{}'"},
                "cron_schedules": {
                    "updated_at": "DATETIME NOT NULL DEFAULT '1970-01-01 00:00:00'"
                },
                "flow_steps": {
                    "step_number": "INTEGER NOT NULL DEFAULT 0",
                    "tool_name": "VARCHAR(128) NOT NULL DEFAULT ''",
                    "arguments": "JSON NOT NULL DEFAULT '{}'",
                },
            }
            for table, declarations in additions.items():
                columns = {
                    row[1]
                    for row in connection.exec_driver_sql(f"PRAGMA table_info({table})")
                }
                for name, declaration in declarations.items():
                    if columns and name not in columns:
                        connection.exec_driver_sql(
                            f"ALTER TABLE {table} ADD COLUMN {name} {declaration}"
                        )
            cron_columns = {
                row[1]
                for row in connection.exec_driver_sql("PRAGMA table_info(cron_schedules)")
            }
            if "updated_at" in cron_columns:
                connection.exec_driver_sql(
                    "UPDATE cron_schedules SET updated_at = created_at "
                    "WHERE updated_at = '1970-01-01 00:00:00'"
                )
            self._tighten_schedule_events_created_at(connection)
            self._tighten_verifier_criteria_task_id(connection)

    @staticmethod
    def _tighten_schedule_events_created_at(connection: Any) -> None:
        info = list(connection.exec_driver_sql("PRAGMA table_info(schedule_events)"))
        created_at = next((row for row in info if row[1] == "created_at"), None)
        if created_at is None or bool(created_at[3]):
            return
        connection.exec_driver_sql(
            "CREATE TABLE schedule_events_v9 ("
            "id VARCHAR(36) NOT NULL PRIMARY KEY, "
            "schedule_id VARCHAR(36) NOT NULL, event_type VARCHAR(16) NOT NULL, "
            "message TEXT, correlation_id VARCHAR(36) NOT NULL DEFAULT '', "
            "created_at DATETIME NOT NULL)"
        )
        connection.exec_driver_sql(
            "INSERT INTO schedule_events_v9 "
            "SELECT id, schedule_id, event_type, message, correlation_id, "
            "COALESCE(created_at, CURRENT_TIMESTAMP) FROM schedule_events"
        )
        connection.exec_driver_sql("DROP TABLE schedule_events")
        connection.exec_driver_sql("ALTER TABLE schedule_events_v9 RENAME TO schedule_events")
        connection.exec_driver_sql(
            "CREATE INDEX ix_schedule_events_schedule_id ON schedule_events (schedule_id)"
        )

    @staticmethod
    def _tighten_verifier_criteria_task_id(connection: Any) -> None:
        info = list(connection.exec_driver_sql("PRAGMA table_info(verifier_criteria)"))
        task_id = next((row for row in info if row[1] == "task_id"), None)
        if task_id is None or bool(task_id[3]):
            return
        null_count = connection.exec_driver_sql(
            "SELECT COUNT(*) FROM verifier_criteria WHERE task_id IS NULL"
        ).scalar_one()
        if null_count:
            raise RuntimeError(
                "cannot upgrade verifier_criteria: NULL task_id has no safe canonical identity"
            )
        connection.exec_driver_sql(
            "CREATE TABLE verifier_criteria_v9 ("
            "task_id VARCHAR(36) NOT NULL PRIMARY KEY "
            "REFERENCES task_flows(id) ON DELETE CASCADE, "
            "criteria TEXT NOT NULL, "
            "created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        connection.exec_driver_sql(
            "INSERT INTO verifier_criteria_v9(task_id, criteria, created_at) "
            "SELECT task_id, criteria, created_at FROM verifier_criteria"
        )
        connection.exec_driver_sql("DROP TABLE verifier_criteria")
        connection.exec_driver_sql(
            "ALTER TABLE verifier_criteria_v9 RENAME TO verifier_criteria"
        )

    def _ensure_approval_grant_column(self) -> None:
        """Replay-safe SQLite upgrade for ``approvals.grant_token`` (v10)."""
        if self.engine.dialect.name != "sqlite":
            return
        with self.engine.begin() as connection:
            columns = {
                row[1]
                for row in connection.exec_driver_sql("PRAGMA table_info(approvals)")
            }
            if columns and "grant_token" not in columns:
                connection.exec_driver_sql(
                    "ALTER TABLE approvals ADD COLUMN grant_token VARCHAR(128)"
                )
            elif columns and "grant_token" in columns:
                # 7B-3: null out legacy raw tokens (length != 64) so raw tokens are not persisted
                connection.exec_driver_sql(
                    "UPDATE approvals SET grant_token = NULL WHERE grant_token IS NOT NULL AND length(grant_token) != 64"
                )

    def _ensure_goal_autonomy_columns(self) -> None:
        """Replay-safe SQLite upgrade for structured M2 autonomy fields."""
        if self.engine.dialect.name != "sqlite":
            return
        with self.engine.begin() as connection:
            columns = {
                row[1] for row in connection.exec_driver_sql("PRAGMA table_info(goals)")
            }
            if not columns:
                return
            additions = {
                "workspace": "TEXT NOT NULL DEFAULT ''",
                "mutation_required": "BOOLEAN NOT NULL DEFAULT 0",
                "test_command": "JSON NOT NULL DEFAULT '[]'",
            }
            for name, declaration in additions.items():
                if name not in columns:
                    connection.exec_driver_sql(
                        f"ALTER TABLE goals ADD COLUMN {name} {declaration}"
                    )

    def session(self) -> Generator[Session, None, None]:
        with self.session_factory() as session:
            yield session

    def dispose(self) -> None:
        """Release all pooled database connections."""
        self.engine.dispose()
