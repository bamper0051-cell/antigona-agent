"""Privacy-safe structured dialogue trace collection.

Captures conversation turns, intent decisions, and workflow events as
structured, redacted records.  No secrets in logs or traces.

Every trace entry goes through the same redact() pipeline used by
antigona.observability before being committed to the trace store.

Usage::

    tracer = TraceCollector()                          # default SQLite store
    tracer = TraceCollector(backend=TraceBackend.JSON)  # JSON-lines on disk

    tracer.record_turn(
        session_id="s1",
        correlation_id="cid1",
        user_text="Hello",
        intent="conversation.greeting",
        response="Hi!",
        duration_ms=12,
    )
    tracer.record_intent(
        correlation_id="cid2",
        intent="task.file_write",
        confidence=0.95,
        reason_code="shell_prefix",
    )
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from antigona.core import paths
from antigona.observability import redact

_log = logging.getLogger(__name__)

#: Default trace stores are governed RUNTIME paths (never the read-only code
#: root).  They are resolved lazily so importing this module never fails closed
#: on a misconfigured deployment; the store is only opened on first use.
def _default_trace_db() -> str:
    return str(paths.traces_db())


def _default_trace_log() -> str:
    return str(paths.traces_log())

TRACE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS traces (
    id              TEXT PRIMARY KEY,
    session_id      TEXT NOT NULL,
    correlation_id  TEXT NOT NULL DEFAULT '',
    trace_type      TEXT NOT NULL,       -- "turn" | "intent" | "workflow"
    timestamp       TEXT NOT NULL,       -- ISO-8601 UTC
    payload         TEXT NOT NULL,       -- JSON, already redacted
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_traces_session ON traces(session_id);
CREATE INDEX IF NOT EXISTS idx_traces_correlation ON traces(correlation_id);
CREATE INDEX IF NOT EXISTS idx_traces_type ON traces(trace_type);
"""


class TraceBackend(StrEnum):
    SQLITE = "sqlite"
    JSON = "json"


class TraceType(StrEnum):
    TURN = "turn"
    INTENT = "intent"
    WORKFLOW = "workflow"


@dataclass
class TraceEntry:
    """A single structured trace record — redacted before storage."""

    id: str = ""
    session_id: str = ""
    correlation_id: str = ""
    trace_type: str = ""
    timestamp: str = ""
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class TurnPayload:
    """Payload for a conversation-turn trace."""

    user_text: str = ""
    intent: str = ""
    response: str = ""
    duration_ms: float = 0.0
    requires_approval: bool = False
    entities: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


@dataclass
class IntentPayload:
    """Payload for an intent-classification trace."""

    intent: str = ""
    confidence: float = 0.0
    reason_code: str = ""
    response_mode: str = ""
    entities: dict[str, Any] = field(default_factory=dict)


@dataclass
class WorkflowPayload:
    """Payload for a workflow (task) trace."""

    task_id: str = ""
    action: str = ""  # created | approved | denied | executing | completed | failed
    tool_name: str = ""
    target_path: str = ""
    duration_ms: float = 0.0
    success: bool = True
    error: str | None = None


class TraceStore:
    """Abstract trace storage backend."""

    def store(self, entry: TraceEntry) -> None:
        """Persist one trace entry."""
        raise NotImplementedError

    def query(
        self,
        session_id: str | None = None,
        trace_type: str | None = None,
        limit: int = 100,
    ) -> list[TraceEntry]:
        """Query stored traces."""
        raise NotImplementedError

    def count(self) -> int:
        """Total stored trace count."""
        raise NotImplementedError

    def close(self) -> None:
        """Release backend resources."""
        pass


class SqliteTraceStore(TraceStore):
    """SQLite-backed trace storage — thread-safe via exclusive connection."""

    def __init__(self, db_path: str | None = None) -> None:
        db_path = db_path or _default_trace_db()
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.executescript(TRACE_SCHEMA_SQL)
        self._conn.commit()

    def store(self, entry: TraceEntry) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO traces (id, session_id, correlation_id, trace_type, timestamp, payload) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                entry.id,
                entry.session_id,
                entry.correlation_id,
                entry.trace_type,
                entry.timestamp,
                json.dumps(entry.payload, sort_keys=True, default=str, separators=(",", ":")),
            ),
        )
        self._conn.commit()

    def query(
        self,
        session_id: str | None = None,
        trace_type: str | None = None,
        limit: int = 100,
    ) -> list[TraceEntry]:
        sql = "SELECT id, session_id, correlation_id, trace_type, timestamp, payload FROM traces WHERE 1=1"
        params: list[Any] = []
        if session_id:
            sql += " AND session_id = ?"
            params.append(session_id)
        if trace_type:
            sql += " AND trace_type = ?"
            params.append(trace_type)
        sql += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)
        rows = self._conn.execute(sql, params).fetchall()
        return [
            TraceEntry(
                id=row[0],
                session_id=row[1],
                correlation_id=row[2],
                trace_type=row[3],
                timestamp=row[4],
                payload=json.loads(row[5]),
            )
            for row in rows
        ]

    def count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) FROM traces").fetchone()
        return row[0] if row else 0

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> SqliteTraceStore:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


class JsonTraceStore(TraceStore):
    """JSON-lines trace storage — append-only log."""

    def __init__(self, path: str | None = None) -> None:
        path = path or _default_trace_log()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._path = path
        self._entries: list[TraceEntry] = []

    def store(self, entry: TraceEntry) -> None:
        self._entries.append(entry)
        with open(self._path, "a") as f:
            f.write(
                json.dumps(
                    {
                        "id": entry.id,
                        "session_id": entry.session_id,
                        "correlation_id": entry.correlation_id,
                        "trace_type": entry.trace_type,
                        "timestamp": entry.timestamp,
                        "payload": entry.payload,
                    },
                    sort_keys=True,
                    default=str,
                    separators=(",", ":"),
                )
                + "\n"
            )

    def query(
        self,
        session_id: str | None = None,
        trace_type: str | None = None,
        limit: int = 100,
    ) -> list[TraceEntry]:
        results: list[TraceEntry] = []
        for e in reversed(self._entries):
            if session_id and e.session_id != session_id:
                continue
            if trace_type and e.trace_type != trace_type:
                continue
            results.append(e)
            if len(results) >= limit:
                break
        return results

    def count(self) -> int:
        return len(self._entries)

    def close(self) -> None:
        pass


class TraceCollector:
    """Privacy-safe trace collector for dialogue turns, intents, and workflows.

    Every payload is redacted before storage.  Default backend is SQLite.

    Typical usage::

        tracer = TraceCollector()
        tracer.record_turn(session_id="s1", correlation_id="c1",
                           user_text="Hello", intent="greeting", response="Hi!")
        all_traces = tracer.query(limit=50)
        print(tracer.stats())
    """

    def __init__(
        self,
        backend: TraceBackend = TraceBackend.SQLITE,
        sqlite_path: str | None = None,
        json_path: str | None = None,
    ) -> None:
        if backend == TraceBackend.SQLITE:
            self._store: TraceStore = SqliteTraceStore(sqlite_path or _default_trace_db())
        else:
            self._store = JsonTraceStore(json_path or _default_trace_log())

    # ── Recording methods ─────────────────────────────────────────────────

    def record_turn(
        self,
        session_id: str,
        correlation_id: str,
        user_text: str,
        intent: str,
        response: str,
        duration_ms: float = 0.0,
        requires_approval: bool = False,
        entities: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> str:
        """Record one conversation turn.  Returns the trace entry id."""
        entry_id = str(uuid.uuid4())
        payload = TurnPayload(
            user_text=user_text,
            intent=intent,
            response=response,
            duration_ms=duration_ms,
            requires_approval=requires_approval,
            entities=entities or {},
            error=error,
        )
        entry = TraceEntry(
            id=entry_id,
            session_id=session_id,
            correlation_id=correlation_id,
            trace_type=TraceType.TURN,
            timestamp=datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            payload=redact(asdict(payload)),  # type: ignore[arg-type]
        )
        self._store.store(entry)
        return entry_id

    def record_intent(
        self,
        correlation_id: str,
        intent: str,
        confidence: float = 0.0,
        reason_code: str = "",
        response_mode: str = "",
        entities: dict[str, Any] | None = None,
    ) -> str:
        """Record one intent-classification event.  Returns the trace entry id."""
        entry_id = str(uuid.uuid4())
        payload = IntentPayload(
            intent=intent,
            confidence=confidence,
            reason_code=reason_code,
            response_mode=response_mode,
            entities=entities or {},
        )
        entry = TraceEntry(
            id=entry_id,
            session_id="",
            correlation_id=correlation_id,
            trace_type=TraceType.INTENT,
            timestamp=datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            payload=redact(asdict(payload)),  # type: ignore[arg-type]
        )
        self._store.store(entry)
        return entry_id

    def record_workflow(
        self,
        session_id: str,
        correlation_id: str,
        task_id: str,
        action: str,
        tool_name: str = "",
        target_path: str = "",
        duration_ms: float = 0.0,
        success: bool = True,
        error: str | None = None,
    ) -> str:
        """Record one workflow/task event.  Returns the trace entry id."""
        entry_id = str(uuid.uuid4())
        payload = WorkflowPayload(
            task_id=task_id,
            action=action,
            tool_name=tool_name,
            target_path=target_path,
            duration_ms=duration_ms,
            success=success,
            error=error,
        )
        entry = TraceEntry(
            id=entry_id,
            session_id=session_id,
            correlation_id=correlation_id,
            trace_type=TraceType.WORKFLOW,
            timestamp=datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            payload=redact(asdict(payload)),  # type: ignore[arg-type]
        )
        self._store.store(entry)
        return entry_id

    # ── Query methods ────────────────────────────────────────────────────

    def query(
        self,
        session_id: str | None = None,
        trace_type: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return traces as dicts (redacted payload already persisted)."""
        return [
            {
                "id": e.id,
                "session_id": e.session_id,
                "correlation_id": e.correlation_id,
                "trace_type": e.trace_type,
                "timestamp": e.timestamp,
                "payload": e.payload,
            }
            for e in self._store.query(
                session_id=session_id, trace_type=trace_type, limit=limit
            )
        ]

    def stats(self) -> dict[str, int | dict[str, int]]:
        """Return summary statistics about stored traces."""
        all_entries = self._store.query(limit=100000)
        total = len(all_entries)
        by_type: dict[str, int] = {}
        for e in all_entries:
            by_type[e.trace_type] = by_type.get(e.trace_type, 0) + 1
        return {
            "total": total,
            "by_type": by_type,
            "unique_sessions": len({e.session_id for e in all_entries if e.session_id}),
        }

    def close(self) -> None:
        self._store.close()
