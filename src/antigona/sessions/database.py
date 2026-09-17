"""SQLite database wrapper for session persistence with crash-safe writes.

Tables:
    sessions    — top-level conversation sessions
    messages    — individual user/assistant messages
    decisions   — router decisions (intent classifications)
    task_refs   — references to task flows created during a session

WAL journal mode + synchronous=NORMAL for crash safety without fsync on every write.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ─── Schema DDL ─────────────────────────────────────────────────────────────────

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sessions (
    id          TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
    title       TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'active'
);

CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    role            TEXT NOT NULL,
    content         TEXT NOT NULL DEFAULT '',
    intent          TEXT NOT NULL DEFAULT '',
    correlation_id  TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_messages_session
    ON messages(session_id, created_at);

CREATE TABLE IF NOT EXISTS decisions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    intent          TEXT NOT NULL,
    confidence      REAL NOT NULL DEFAULT 0.0,
    entities_json   TEXT NOT NULL DEFAULT '{}',
    reason_code     TEXT NOT NULL DEFAULT '',
    response_mode   TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_decisions_session
    ON decisions(session_id, created_at);

CREATE TABLE IF NOT EXISTS task_refs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    flow_id     TEXT NOT NULL,
    tool_name   TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'created',
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_task_refs_session
    ON task_refs(session_id);
"""

WAL_PRAGMAS = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;
PRAGMA busy_timeout=5000;
"""

def _resolve_db_path(path: str | None) -> str:
    """Resolve DB path, defaulting to canonical ``sessions_db_path()``."""
    if path:
        if path == ":memory:" or path.startswith("file:") or path.startswith("sqlite:"):
            return path
        return str(Path(path).resolve())
    from antigona.core import paths
    return str(paths.sessions_db_path().resolve())


class _DefaultDbPathProxy(str):
    def __str__(self) -> str:
        from antigona.core import paths
        return str(paths.sessions_db_path().resolve())

    def __repr__(self) -> str:
        return repr(str(self))

    def __eq__(self, other: object) -> bool:
        return str(self) == str(other)


DEFAULT_DB_PATH = _DefaultDbPathProxy()



class SessionDatabase:
    """Low-level aiosqlite wrapper for the sessions schema.

    Manages a single connection. Not thread-safe — create one per event loop.
    """

    def __init__(self, db_path: str | None = None) -> None:
        self._db_path: str = _resolve_db_path(db_path)
        self._conn: Any = None  # aiosqlite.Connection, set during connect()

    @property
    def db_path(self) -> str:
        return self._db_path

    # ── Lifecycle ───────────────────────────────────────────────────────────

    async def connect(self) -> None:
        """Open (or create) the SQLite database and apply the schema."""
        import aiosqlite

        self._conn = await aiosqlite.connect(self._db_path)
        self._conn.row_factory = sqlite3.Row

        # WAL + crash-safe mode
        for pragma in WAL_PRAGMAS.strip().split(";"):
            pragma = pragma.strip()
            if pragma:
                await self._conn.execute(pragma)

        await self._conn.executescript(SCHEMA_SQL)
        await self._conn.commit()
        logger.info("Session DB opened: %s (WAL+NORMAL)", self._db_path)

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None
            await asyncio.sleep(0)

    async def __aenter__(self) -> SessionDatabase:
        await self.connect()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()

    # ── Sessions ────────────────────────────────────────────────────────────

    async def create_session(
        self, session_id: str, title: str = "", status: str = "active"
    ) -> dict[str, Any]:
        assert self._conn is not None
        await self._conn.execute(
            "INSERT INTO sessions (id, title, status) VALUES (?, ?, ?)",
            (session_id, title, status),
        )
        await self._conn.commit()
        return await self.get_session(session_id)  # type: ignore[return-value]

    async def get_session(self, session_id: str) -> dict[str, Any] | None:
        assert self._conn is not None
        cur = await self._conn.execute(
            "SELECT id, created_at, updated_at, title, status FROM sessions WHERE id = ?",
            (session_id,),
        )
        row = await cur.fetchone()
        if row is None:
            return None
        return dict(row)

    async def list_sessions(
        self, limit: int = 50, offset: int = 0, status: str | None = None
    ) -> list[dict[str, Any]]:
        assert self._conn is not None
        if status:
            cur = await self._conn.execute(
                "SELECT id, created_at, updated_at, title, status FROM sessions "
                "WHERE status = ? ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                (status, limit, offset),
            )
        else:
            cur = await self._conn.execute(
                "SELECT id, created_at, updated_at, title, status FROM sessions "
                "ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def update_session(
        self, session_id: str, title: str | None = None, status: str | None = None
    ) -> dict[str, Any] | None:
        assert self._conn is not None
        sets: list[str] = []
        params: list[Any] = []
        if title is not None:
            sets.append("title = ?")
            params.append(title)
        if status is not None:
            sets.append("status = ?")
            params.append(status)
        if not sets:
            return await self.get_session(session_id)
        sets.append("updated_at = datetime('now')")
        params.append(session_id)
        await self._conn.execute(
            f"UPDATE sessions SET {', '.join(sets)} WHERE id = ?",
            params,
        )
        await self._conn.commit()
        return await self.get_session(session_id)

    async def delete_session(self, session_id: str) -> bool:
        """Delete a session and cascade-delete its messages/decisions/task_refs.

        Returns True if a row was deleted, False if the session did not exist.
        """
        assert self._conn is not None
        cur = await self._conn.execute(
            "DELETE FROM sessions WHERE id = ?",
            (session_id,),
        )
        await self._conn.commit()
        return bool(cur.rowcount > 0)

    # ── Messages ────────────────────────────────────────────────────────────

    async def add_message(
        self,
        session_id: str,
        role: str,
        content: str,
        intent: str = "",
        correlation_id: str = "",
    ) -> dict[str, Any]:
        assert self._conn is not None
        cur = await self._conn.execute(
            "INSERT INTO messages (session_id, role, content, intent, correlation_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (session_id, role, content, intent, correlation_id),
        )
        msg_id = cur.lastrowid
        await self._conn.execute(
            "UPDATE sessions SET updated_at = datetime('now') WHERE id = ?",
            (session_id,),
        )
        await self._conn.commit()
        return await self._get_message(msg_id)  # type: ignore[return-value]

    async def _get_message(self, msg_id: int) -> dict[str, Any] | None:
        assert self._conn is not None
        cur = await self._conn.execute(
            "SELECT id, session_id, role, content, intent, correlation_id, created_at "
            "FROM messages WHERE id = ?",
            (msg_id,),
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def get_messages(
        self,
        session_id: str,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Return the most recent ``limit`` messages in chronological order.

        Fetches newest-first via ``ORDER BY id DESC`` then reverses so the
        result is oldest→newest (the last N messages). ``offset`` pages
        backwards from the most recent (offset=0 → the last N). Fixes P-01:
        previously ``ORDER BY id ASC`` returned the session's FIRST messages,
        so long sessions lost recent context in the model context packet.
        """
        assert self._conn is not None
        cur = await self._conn.execute(
            "SELECT id, session_id, role, content, intent, correlation_id, created_at "
            "FROM messages WHERE session_id = ? ORDER BY id DESC LIMIT ? OFFSET ?",
            (session_id, limit, offset),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in reversed(rows)]

    # ── Decisions ───────────────────────────────────────────────────────────

    async def add_decision(
        self,
        session_id: str,
        intent: str,
        confidence: float = 0.0,
        entities_json: str = "{}",
        reason_code: str = "",
        response_mode: str = "",
    ) -> dict[str, Any]:
        assert self._conn is not None
        cur = await self._conn.execute(
            "INSERT INTO decisions (session_id, intent, confidence, entities_json, "
            "reason_code, response_mode) VALUES (?, ?, ?, ?, ?, ?)",
            (session_id, intent, confidence, entities_json, reason_code, response_mode),
        )
        dec_id = cur.lastrowid
        await self._conn.commit()
        return await self._get_decision(dec_id)  # type: ignore[return-value]

    async def _get_decision(self, dec_id: int) -> dict[str, Any] | None:
        assert self._conn is not None
        cur = await self._conn.execute(
            "SELECT id, session_id, intent, confidence, entities_json, reason_code, "
            "response_mode, created_at FROM decisions WHERE id = ?",
            (dec_id,),
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def get_decisions(
        self,
        session_id: str,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        assert self._conn is not None
        cur = await self._conn.execute(
            "SELECT id, session_id, intent, confidence, entities_json, reason_code, "
            "response_mode, created_at FROM decisions WHERE session_id = ? "
            "ORDER BY id ASC LIMIT ? OFFSET ?",
            (session_id, limit, offset),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    # ── Task refs ───────────────────────────────────────────────────────────

    async def add_task_ref(
        self,
        session_id: str,
        flow_id: str,
        tool_name: str = "",
        status: str = "created",
    ) -> dict[str, Any]:
        assert self._conn is not None
        cur = await self._conn.execute(
            "INSERT INTO task_refs (session_id, flow_id, tool_name, status) "
            "VALUES (?, ?, ?, ?)",
            (session_id, flow_id, tool_name, status),
        )
        ref_id = cur.lastrowid
        await self._conn.commit()
        return await self._get_task_ref(ref_id)  # type: ignore[return-value]

    async def _get_task_ref(self, ref_id: int) -> dict[str, Any] | None:
        assert self._conn is not None
        cur = await self._conn.execute(
            "SELECT id, session_id, flow_id, tool_name, status, created_at "
            "FROM task_refs WHERE id = ?",
            (ref_id,),
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def update_task_ref(
        self, ref_id: int, status: str
    ) -> dict[str, Any] | None:
        assert self._conn is not None
        await self._conn.execute(
            "UPDATE task_refs SET status = ? WHERE id = ?",
            (status, ref_id),
        )
        await self._conn.commit()
        return await self._get_task_ref(ref_id)

    async def get_task_refs(
        self,
        session_id: str,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        assert self._conn is not None
        cur = await self._conn.execute(
            "SELECT id, session_id, flow_id, tool_name, status, created_at "
            "FROM task_refs WHERE session_id = ? "
            "ORDER BY id ASC LIMIT ? OFFSET ?",
            (session_id, limit, offset),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    # ── Convenience ─────────────────────────────────────────────────────────

    async def session_exists(self, session_id: str) -> bool:
        """Check if a session exists without loading all its data."""
        assert self._conn is not None
        cur = await self._conn.execute(
            "SELECT 1 FROM sessions WHERE id = ?", (session_id,)
        )
        return await cur.fetchone() is not None


# ─── Module-level helper ───────────────────────────────────────────────────────


async def get_db(path: str | None = None) -> AsyncGenerator[SessionDatabase, None]:
    """Context manager helper that yields a connected SessionDatabase."""
    db = SessionDatabase(path)
    try:
        await db.connect()
        yield db
    finally:
        await db.close()
