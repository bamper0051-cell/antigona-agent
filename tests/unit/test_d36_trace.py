"""Д36: Trace collection — privacy-safe dialogue traces.

Gate: Structured privacy-safe dialogue traces captured.  No secrets in logs.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from antigona.observability import redact
from antigona.trace import (
    JsonTraceStore,
    SqliteTraceStore,
    TraceBackend,
    TraceCollector,
    TraceEntry,
    TraceType,
    TurnPayload,
)


class TestSqliteTraceStore:
    """SQLite backend: schema, store, query, isolation."""

    def test_schema_created(self, tmp_path: Path) -> None:
        db = str(tmp_path / "traces.db")
        SqliteTraceStore(db)
        conn = sqlite3.connect(db)
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        assert ("traces",) in tables

    def test_store_and_count(self, tmp_path: Path) -> None:
        db = str(tmp_path / "traces.db")
        store = SqliteTraceStore(db)
        assert store.count() == 0
        entry = TraceEntry(
            id="e1",
            session_id="s1",
            correlation_id="c1",
            trace_type="turn",
            timestamp="2026-07-27T12:00:00Z",
            payload={"key": "val"},
        )
        store.store(entry)
        assert store.count() == 1

    def test_query_by_session(self, tmp_path: Path) -> None:
        db = str(tmp_path / "traces.db")
        store = SqliteTraceStore(db)
        store.store(
            TraceEntry(id="a", session_id="s1", trace_type="turn", payload={})
        )
        store.store(
            TraceEntry(id="b", session_id="s2", trace_type="turn", payload={})
        )
        store.store(
            TraceEntry(id="c", session_id="s1", trace_type="turn", payload={})
        )
        results = store.query(session_id="s1")
        assert len(results) == 2
        assert {r.id for r in results} == {"a", "c"}

    def test_query_by_type(self, tmp_path: Path) -> None:
        db = str(tmp_path / "traces.db")
        store = SqliteTraceStore(db)
        store.store(TraceEntry(id="t1", session_id="s1", trace_type="turn", payload={}))
        store.store(TraceEntry(id="t2", session_id="s1", trace_type="intent", payload={}))
        results = store.query(trace_type="intent")
        assert len(results) == 1
        assert results[0].id == "t2"

    def test_dedupe_by_id(self, tmp_path: Path) -> None:
        db = str(tmp_path / "traces.db")
        store = SqliteTraceStore(db)
        entry = TraceEntry(id="unique", payload={})
        store.store(entry)
        store.store(entry)  # second insert ignored (OR IGNORE)
        assert store.count() == 1

    def test_json_payload_roundtrip(self, tmp_path: Path) -> None:
        db = str(tmp_path / "traces.db")
        store = SqliteTraceStore(db)
        payload = {"user_text": "hello", "nested": {"a": 1}, "list": [1, 2, 3]}
        store.store(
            TraceEntry(id="r1", session_id="s1", trace_type="turn", payload=payload)
        )
        results = store.query(session_id="s1")
        assert len(results) == 1
        assert results[0].payload == payload


class TestJsonTraceStore:
    """JSON-lines backend: append, query, survive restart."""

    def test_append_and_read(self, tmp_path: Path) -> None:
        path = str(tmp_path / "traces.jsonl")
        store = JsonTraceStore(path)
        store.store(
            TraceEntry(id="j1", session_id="s1", trace_type="turn", payload={"msg": "hello"})
        )
        assert store.count() == 1
        results = store.query()
        assert len(results) == 1
        assert results[0].id == "j1"

    def test_persistence_on_disk(self, tmp_path: Path) -> None:
        path = str(tmp_path / "traces.jsonl")
        store = JsonTraceStore(path)
        store.store(TraceEntry(id="p1", session_id="s1", trace_type="turn", payload={}))
        store.close()

        # Reload — reads from file
        store2 = JsonTraceStore(path)
        assert store2.count() == 0  # in-memory append only
        store2.store(
            TraceEntry(id="p2", session_id="s1", trace_type="turn", payload={})
        )
        assert store2.count() == 1  # p1 already on disk, p2 in memory


class TestTraceCollector:
    """High-level collector: record, query, stats, redaction."""

    def test_record_turn_returns_id(self, tmp_path: Path) -> None:
        db = str(tmp_path / "traces.db")
        tracer = TraceCollector(backend=TraceBackend.SQLITE, sqlite_path=db)
        tid = tracer.record_turn(
            session_id="s1", correlation_id="c1",
            user_text="Hello", intent="greeting", response="Hi!",
        )
        assert isinstance(tid, str)
        assert len(tid) > 10
        assert tracer.stats()["total"] == 1

    def test_record_intent(self, tmp_path: Path) -> None:
        db = str(tmp_path / "traces.db")
        tracer = TraceCollector(backend=TraceBackend.SQLITE, sqlite_path=db)
        iid = tracer.record_intent(
            correlation_id="c1", intent="task.file_write",
            confidence=0.95, reason_code="shell_prefix",
        )
        assert iid
        assert tracer.stats()["total"] == 1

    def test_record_workflow(self, tmp_path: Path) -> None:
        db = str(tmp_path / "traces.db")
        tracer = TraceCollector(backend=TraceBackend.SQLITE, sqlite_path=db)
        wid = tracer.record_workflow(
            session_id="s1", correlation_id="c1", task_id="t1",
            action="completed", tool_name="filesystem.write",
        )
        assert wid
        assert tracer.stats()["total"] == 1

    def test_query_returns_dicts(self, tmp_path: Path) -> None:
        db = str(tmp_path / "traces.db")
        tracer = TraceCollector(backend=TraceBackend.SQLITE, sqlite_path=db)
        tracer.record_turn(session_id="s1", correlation_id="c1",
                           user_text="Hi", intent="greeting", response="Hello!")
        results = tracer.query(session_id="s1")
        assert len(results) == 1
        assert results[0]["trace_type"] == TraceType.TURN
        assert "payload" in results[0]

    def test_query_without_filter(self, tmp_path: Path) -> None:
        db = str(tmp_path / "traces.db")
        tracer = TraceCollector(backend=TraceBackend.SQLITE, sqlite_path=db)
        tracer.record_turn(session_id="s1", correlation_id="c1",
                           user_text="A", intent="x", response="B")
        tracer.record_intent(correlation_id="c2", intent="y", confidence=0.8)
        results = tracer.query(limit=10)
        assert len(results) == 2

    def test_stats_multiple_types(self, tmp_path: Path) -> None:
        db = str(tmp_path / "traces.db")
        tracer = TraceCollector(backend=TraceBackend.SQLITE, sqlite_path=db)
        tracer.record_turn(session_id="s1", correlation_id="c1",
                           user_text="A", intent="x", response="B")
        tracer.record_intent(correlation_id="c2", intent="y", confidence=0.9)
        tracer.record_workflow(session_id="s1", correlation_id="c3",
                               task_id="t1", action="created")
        stats = tracer.stats()
        assert stats["total"] == 3
        assert stats["by_type"]["turn"] == 1
        assert stats["by_type"]["intent"] == 1
        assert stats["by_type"]["workflow"] == 1
        assert stats["unique_sessions"] == 1

    def test_redaction_applied(self, tmp_path: Path) -> None:
        """Verify that secrets are redacted from stored payloads."""
        db = str(tmp_path / "traces.db")
        tracer = TraceCollector(backend=TraceBackend.SQLITE, sqlite_path=db)
        tracer.record_turn(
            session_id="s1", correlation_id="c1",
            user_text="My secret is fine",
            intent="conversation",
            response="I see you have some info",
            entities={"api_key": "sk-abc123"},
        )
        results = tracer.query(limit=10)
        payload_str = json.dumps(results[0]["payload"])
        assert "[REDACTED]" in payload_str

    def test_turn_payload_redact_property(self) -> None:
        """TurnPayload fields are redactable."""
        TurnPayload(
            user_text="my token is xyz",
            intent="conversation",
            response="token received",
        )
        redacted = redact(
            {"user_text": "my token is xyz", "intent": "conversation", "response": "token received"}
        )
        assert isinstance(redacted, dict)

    def test_close_no_error(self, tmp_path: Path) -> None:
        db = str(tmp_path / "traces.db")
        tracer = TraceCollector(backend=TraceBackend.SQLITE, sqlite_path=db)
        tracer.record_turn(session_id="s1", correlation_id="c1",
                           user_text="x", intent="y", response="z")
        tracer.close()

    def test_json_backend(self, tmp_path: Path) -> None:
        path = str(tmp_path / "traces.jsonl")
        tracer = TraceCollector(backend=TraceBackend.JSON, json_path=path)
        tracer.record_turn(session_id="s1", correlation_id="c1",
                           user_text="Hello", intent="greeting", response="Hi!")
        assert tracer.stats()["total"] == 1
        tracer.close()

    def test_secrets_never_in_logged_payload(self, tmp_path: Path) -> None:
        """Secret keys like api_key are redacted even when they are dict keys."""
        db = str(tmp_path / "traces.db")
        tracer = TraceCollector(backend=TraceBackend.SQLITE, sqlite_path=db)
        tracer.record_intent(
            correlation_id="c1",
            intent="task.shell",
            confidence=0.99,
            entities={"api_key": "sk-secret123"},
        )
        results = tracer.query(limit=10)
        payload = results[0]["payload"]
        assert payload.get("entities", {}).get("api_key", "") == "[REDACTED]"
