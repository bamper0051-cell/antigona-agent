"""Day 10 — Unit tests for session persistence (SQLite, crash-safe, WAL).

Tests cover:
  - SessionDatabase: create / get / list / update / session_exists
  - SessionRepository: create_session / get_session / list_sessions
  - add_message / get_messages
  - add_decision / get_decisions
  - add_task_ref / update_task_ref
  - Concurrent safety (multiple coroutines writing to the same DB)
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from collections.abc import AsyncGenerator

import pytest

from antigona.sessions.database import SessionDatabase
from antigona.sessions.repository import SessionRepository

# ─── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
async def tmp_db_path() -> AsyncGenerator[str, None]:
    """Return a temporary file path for the session DB."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    yield path
    try:
        os.unlink(path)
        # Clean up WAL/SHM files
        for ext in ("-wal", "-shm"):
            try:
                os.unlink(path + ext)
            except FileNotFoundError:
                pass
    except FileNotFoundError:
        pass


@pytest.fixture
async def db(tmp_db_path: str) -> AsyncGenerator[SessionDatabase, None]:
    """Return a connected SessionDatabase with clean schema."""
    db = SessionDatabase(tmp_db_path)
    await db.connect()
    yield db
    await db.close()


@pytest.fixture
async def repo(tmp_db_path: str) -> AsyncGenerator[SessionRepository, None]:
    """Return a connected SessionRepository with clean schema."""
    repo = SessionRepository(tmp_db_path)
    await repo.connect()
    yield repo
    await repo.close()


# ─── SessionDatabase: low-level tests ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_db_connect_creates_tables(db: SessionDatabase) -> None:
    """Verify that connect() creates all expected tables."""
    cur = await db._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )
    tables = {row["name"] for row in await cur.fetchall()}
    assert "sessions" in tables
    assert "messages" in tables
    assert "decisions" in tables
    assert "task_refs" in tables


@pytest.mark.asyncio
async def test_db_wal_mode(db: SessionDatabase) -> None:
    """Verify WAL journal mode is active."""
    cur = await db._conn.execute("PRAGMA journal_mode")
    row = await cur.fetchone()
    assert row[0].upper() == "WAL"


@pytest.mark.asyncio
async def test_db_sync_normal(db: SessionDatabase) -> None:
    """Verify synchronous=NORMAL for crash safety."""
    cur = await db._conn.execute("PRAGMA synchronous")
    row = await cur.fetchone()
    assert row[0] == 1  # 1 = NORMAL in SQLite


@pytest.mark.asyncio
async def test_db_foreign_keys_on(db: SessionDatabase) -> None:
    """Verify foreign keys are enabled."""
    cur = await db._conn.execute("PRAGMA foreign_keys")
    row = await cur.fetchone()
    assert row[0] == 1


@pytest.mark.asyncio
async def test_db_create_and_get_session(db: SessionDatabase) -> None:
    """Create a session and retrieve it by ID."""
    created = await db.create_session("test-1", title="Test Session", status="active")
    assert created["id"] == "test-1"
    assert created["title"] == "Test Session"
    assert created["status"] == "active"
    assert created["created_at"] is not None

    fetched = await db.get_session("test-1")
    assert fetched is not None
    assert fetched["id"] == "test-1"
    assert fetched["title"] == "Test Session"


@pytest.mark.asyncio
async def test_db_get_session_not_found(db: SessionDatabase) -> None:
    """get_session returns None for non-existent session."""
    result = await db.get_session("nonexistent")
    assert result is None


@pytest.mark.asyncio
async def test_db_list_sessions(db: SessionDatabase) -> None:
    """List sessions, newest first."""
    await db.create_session("sess-1", title="First")
    await db.create_session("sess-2", title="Second")
    sessions = await db.list_sessions(limit=10)
    assert len(sessions) >= 2
    # Both sessions should be present
    ids = {s["id"] for s in sessions}
    assert "sess-1" in ids
    assert "sess-2" in ids


@pytest.mark.asyncio
async def test_db_list_sessions_with_status_filter(db: SessionDatabase) -> None:
    """Filter sessions by status."""
    await db.create_session("sess-active", title="Active", status="active")
    await db.create_session("sess-archived", title="Archived", status="archived")

    active = await db.list_sessions(status="active")
    assert len(active) == 1
    assert active[0]["id"] == "sess-active"

    archived = await db.list_sessions(status="archived")
    assert len(archived) == 1
    assert archived[0]["id"] == "sess-archived"


@pytest.mark.asyncio
async def test_db_update_session(db: SessionDatabase) -> None:
    """Update session title and status."""
    await db.create_session("sess-upd", title="Original", status="active")
    updated = await db.update_session("sess-upd", title="Updated", status="archived")
    assert updated is not None
    assert updated["title"] == "Updated"
    assert updated["status"] == "archived"


@pytest.mark.asyncio
async def test_db_session_exists(db: SessionDatabase) -> None:
    """Check session existence."""
    await db.create_session("sess-exist")
    assert await db.session_exists("sess-exist") is True
    assert await db.session_exists("sess-nope") is False


# ─── Messages ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_db_add_and_get_messages(db: SessionDatabase) -> None:
    """Add a message and retrieve it."""
    await db.create_session("msg-sess")
    msg = await db.add_message(
        "msg-sess",
        role="user",
        content="Hello, bot!",
        intent="conversation.greeting",
        correlation_id="corr-001",
    )
    assert msg["session_id"] == "msg-sess"
    assert msg["role"] == "user"
    assert msg["content"] == "Hello, bot!"
    assert msg["intent"] == "conversation.greeting"
    assert msg["correlation_id"] == "corr-001"

    msgs = await db.get_messages("msg-sess")
    assert len(msgs) == 1
    assert msgs[0]["content"] == "Hello, bot!"


@pytest.mark.asyncio
async def test_db_get_messages_empty_session(db: SessionDatabase) -> None:
    """get_messages returns empty list for session with no messages."""
    await db.create_session("empty-sess")
    msgs = await db.get_messages("empty-sess")
    assert msgs == []


@pytest.mark.asyncio
async def test_db_get_messages_ordering(db: SessionDatabase) -> None:
    """Messages are returned in insertion order."""
    await db.create_session("order-sess")
    for i in range(5):
        await db.add_message("order-sess", role="user", content=f"msg-{i}")
    msgs = await db.get_messages("order-sess", limit=5)
    contents = [m["content"] for m in msgs]
    assert contents == ["msg-0", "msg-1", "msg-2", "msg-3", "msg-4"]



@pytest.mark.asyncio
async def test_db_get_messages_returns_most_recent_in_chronological_order(
    db: SessionDatabase,
) -> None:
    """P-01 regression: get_messages returns the LAST N messages, not the first N.

    Previously ``ORDER BY id ASC LIMIT N OFFSET 0`` returned the session's FIRST
    messages, so long sessions lost recent context in the model context packet.
    """
    await db.create_session("recent-sess")
    for i in range(10):
        await db.add_message("recent-sess", role="user", content=f"msg-{i}")

    # default limit=100 → all messages in chronological order
    all_msgs = await db.get_messages("recent-sess")
    assert [m["content"] for m in all_msgs] == [f"msg-{i}" for i in range(10)]

    # limit=5 → the most recent 5, in chronological order
    recent = await db.get_messages("recent-sess", limit=5)
    assert [m["content"] for m in recent] == [
        "msg-5", "msg-6", "msg-7", "msg-8", "msg-9",
    ]

    # offset pages backwards from the most recent
    older = await db.get_messages("recent-sess", limit=5, offset=5)
    assert [m["content"] for m in older] == [
        "msg-0", "msg-1", "msg-2", "msg-3", "msg-4",
    ]


# ─── Decisions ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_db_add_and_get_decisions(db: SessionDatabase) -> None:
    """Add a decision and retrieve it."""
    await db.create_session("dec-sess")
    dec = await db.add_decision(
        "dec-sess",
        intent="task.file_write",
        confidence=0.95,
        entities_json='{"path": "/tmp/test.txt"}',
        reason_code="file_write_match",
        response_mode="task_preview",
    )
    assert dec["session_id"] == "dec-sess"
    assert dec["intent"] == "task.file_write"
    assert dec["confidence"] == 0.95
    assert dec["reason_code"] == "file_write_match"

    decs = await db.get_decisions("dec-sess")
    assert len(decs) == 1
    assert decs[0]["intent"] == "task.file_write"


@pytest.mark.asyncio
async def test_db_decision_entities_json(db: SessionDatabase) -> None:
    """Decision entities are stored as JSON string."""
    await db.create_session("dec-entities")
    entities = {"path": "/etc/config", "content": "server { listen 80; }"}
    await db.add_decision(
        "dec-entities",
        intent="task.file_write",
        confidence=0.9,
        entities_json=json.dumps(entities, ensure_ascii=False),
    )
    decs = await db.get_decisions("dec-entities")
    assert len(decs) == 1
    stored_entities = json.loads(decs[0]["entities_json"])
    assert stored_entities == entities


# ─── Task refs ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_db_add_and_update_task_ref(db: SessionDatabase) -> None:
    """Add a task ref and update its status."""
    await db.create_session("task-sess")
    ref = await db.add_task_ref("task-sess", flow_id="flow-001", tool_name="workspace.write_text")
    assert ref["session_id"] == "task-sess"
    assert ref["flow_id"] == "flow-001"
    assert ref["status"] == "created"

    updated = await db.update_task_ref(ref["id"], status="completed")
    assert updated is not None
    assert updated["status"] == "completed"
    assert updated["id"] == ref["id"]


@pytest.mark.asyncio
async def test_db_get_task_refs(db: SessionDatabase) -> None:
    """Get all task refs for a session."""
    await db.create_session("task-list")
    await db.add_task_ref("task-list", flow_id="flow-001")
    await db.add_task_ref("task-list", flow_id="flow-002")
    refs = await db.get_task_refs("task-list")
    assert len(refs) == 2
    assert {r["flow_id"] for r in refs} == {"flow-001", "flow-002"}


# ─── Repository (high-level) ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_repo_create_and_get_session(repo: SessionRepository) -> None:
    """Create a session via repo and fetch it."""
    created = await repo.create_session(title="Repo Test")
    assert "id" in created
    assert created["title"] == "Repo Test"

    fetched = await repo.get_session(created["id"])
    assert fetched is not None
    assert fetched["id"] == created["id"]


@pytest.mark.asyncio
async def test_repo_list_sessions(repo: SessionRepository) -> None:
    """List sessions via repo."""
    s1 = await repo.create_session(title="Session A")
    s2 = await repo.create_session(title="Session B")
    sessions = await repo.list_sessions(limit=10)
    ids = {s["id"] for s in sessions}
    assert s1["id"] in ids
    assert s2["id"] in ids


@pytest.mark.asyncio
async def test_repo_add_and_get_messages(repo: SessionRepository) -> None:
    """Add and retrieve messages via repo."""
    sess = await repo.create_session(title="Msg Test")
    msg = await repo.add_message(
        sess["id"],
        role="user",
        content="Test message",
        intent="conversation.greeting",
        correlation_id="cid-001",
    )
    assert msg["content"] == "Test message"

    msgs = await repo.get_messages(sess["id"])
    assert len(msgs) == 1
    assert msgs[0]["correlation_id"] == "cid-001"


@pytest.mark.asyncio
async def test_repo_add_and_get_decisions(repo: SessionRepository) -> None:
    """Add and retrieve decisions via repo (entities serialised internally)."""
    sess = await repo.create_session(title="Dec Test")
    dec = await repo.add_decision(
        sess["id"],
        intent="task.shell",
        confidence=0.88,
        entities={"command": "ls -la"},
        reason_code="shell_prefix",
        response_mode="task_preview",
    )
    assert dec["intent"] == "task.shell"
    assert dec["confidence"] == 0.88

    decs = await repo.get_decisions(sess["id"])
    assert len(decs) == 1
    stored = json.loads(decs[0]["entities_json"])
    assert stored == {"command": "ls -la"}


@pytest.mark.asyncio
async def test_repo_add_and_update_task_ref(repo: SessionRepository) -> None:
    """Add and update a task ref via repo."""
    sess = await repo.create_session(title="TaskRef Test")
    ref = await repo.add_task_ref(sess["id"], flow_id="flow-abc", tool_name="sandbox.shell")
    assert ref["flow_id"] == "flow-abc"
    assert ref["status"] == "created"

    updated = await repo.update_task_ref(ref["id"], status="done")
    assert updated is not None
    assert updated["status"] == "done"


@pytest.mark.asyncio
async def test_repo_session_exists(repo: SessionRepository) -> None:
    """Verify session_exists works."""
    sess = await repo.create_session(title="Exists Check")
    assert await repo.session_exists(sess["id"]) is True
    assert await repo.session_exists("nope") is False


@pytest.mark.asyncio
async def test_repo_update_session(repo: SessionRepository) -> None:
    """Update session via repo."""
    sess = await repo.create_session(title="Original Title")
    updated = await repo.update_session(sess["id"], title="New Title", status="archived")
    assert updated is not None
    assert updated["title"] == "New Title"
    assert updated["status"] == "archived"


# ─── Concurrent safety ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_concurrent_message_writes(tmp_db_path: str) -> None:
    """Multiple coroutines writing messages concurrently to the same DB.

    This verifies crash safety and that the WAL mode handles concurrent
    writers without deadlock or data loss.
    """
    db = SessionDatabase(tmp_db_path)
    await db.connect()
    await db.create_session("concurrent-sess")

    async def write_message(idx: int) -> None:
        for _ in range(10):
            await db.add_message(
                "concurrent-sess",
                role="user",
                content=f"concurrent-msg-{idx}",
                correlation_id=f"corr-{idx}",
            )

    import asyncio

    tasks = [write_message(i) for i in range(5)]
    await asyncio.gather(*tasks)

    msgs = await db.get_messages("concurrent-sess")
    assert len(msgs) == 50  # 5 writers × 10 messages each
    await db.close()


@pytest.mark.asyncio
async def test_cascade_delete(tmp_db_path: str) -> None:
    """Deleting a session via raw SQL cascades to messages, decisions, task_refs."""
    db = SessionDatabase(tmp_db_path)
    await db.connect()
    await db.create_session("cascade-sess")
    await db.add_message("cascade-sess", role="user", content="test")
    await db.add_decision("cascade-sess", intent="test.intent", confidence=0.5)
    await db.add_task_ref("cascade-sess", flow_id="flow-test")

    # Delete session
    await db._conn.execute("DELETE FROM sessions WHERE id = ?", ("cascade-sess",))
    await db._conn.commit()

    msgs = await db.get_messages("cascade-sess")
    decs = await db.get_decisions("cascade-sess")
    refs = await db.get_task_refs("cascade-sess")
    assert msgs == []
    assert decs == []
    assert refs == []
    await db.close()


# ─── Crash safety: WAL + NORMAL pragmas ───────────────────────────────────────


@pytest.mark.asyncio
async def test_db_file_exists(tmp_db_path: str) -> None:
    """Verify the DB file was actually created on disk."""
    db = SessionDatabase(tmp_db_path)
    await db.connect()
    await db.close()
    assert os.path.isfile(tmp_db_path), "SQLite DB file should exist on disk"


@pytest.mark.asyncio
async def test_data_survives_reconnect(tmp_db_path: str) -> None:
    """Data written in one connection is readable in a new connection.

    This verifies that sessions are truly persisted between bot restarts.
    """
    # First connection: write data
    db1 = SessionDatabase(tmp_db_path)
    await db1.connect()
    await db1.create_session("survive-test", title="Persist Check")
    await db1.add_message("survive-test", role="user", content="Hello")
    await db1.close()

    # Second connection: read data (simulating bot restart)
    db2 = SessionDatabase(tmp_db_path)
    await db2.connect()
    sess = await db2.get_session("survive-test")
    assert sess is not None
    assert sess["title"] == "Persist Check"

    msgs = await db2.get_messages("survive-test")
    assert len(msgs) == 1
    assert msgs[0]["content"] == "Hello"
    await db2.close()


# ─── CONFLICT E regression: non-daemon aiosqlite worker release ──────────────


async def test_close_releases_non_daemon_aiosqlite_worker(tmp_db_path) -> None:
    """close() must terminate the aiosqlite worker thread.

    aiosqlite 0.22.1 builds its connection worker thread WITHOUT
    ``daemon=True`` (``core.py: Thread(...)``). If a SessionDatabase is left
    open, ``threading._shutdown`` blocks forever on that thread at
    interpreter exit — the root cause of the previously-stuck bot process.
    This test guards that ``SessionDatabase.close()`` actually releases the
    worker so the process can exit cleanly.
    """
    import threading

    def is_worker(t: threading.Thread) -> bool:
        return "connection_worker" in t.name or "aiosqlite" in type(t).__module__

    # Other tests in a full-suite run may legitimately have aiosqlite workers
    # alive; only verify that THIS connection's worker is created and then
    # released on close (baseline-relative, not global-zero).
    baseline = [t for t in threading.enumerate() if t.is_alive() and is_worker(t)]

    db = SessionDatabase(tmp_db_path)
    await db.connect()
    await db.list_sessions()  # flush any in-flight task so the worker is idle
    await asyncio.sleep(0.2)

    assert len([t for t in threading.enumerate() if t.is_alive() and is_worker(t)]) > len(
        baseline
    ), "precondition: aiosqlite worker thread should be alive after connect()"

    await db.close()
    await asyncio.sleep(0.3)

    leftovers = [
        t
        for t in threading.enumerate()
        if t.is_alive() and is_worker(t) and t not in baseline
    ]
    assert not leftovers, (
        f"close() did not release non-daemon aiosqlite worker: {leftovers}"
    )
