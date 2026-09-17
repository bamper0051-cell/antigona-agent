"""High-level repository for session CRUD operations.

Wraps SessionDatabase with a typed, structured interface for use by the bot.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from antigona.sessions.database import SessionDatabase


def _now_iso() -> str:
    """Return current UTC time as ISO 8601 string sans microseconds."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _make_session_id() -> str:
    """Generate a short, human-readable session ID."""
    ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    suffix = uuid.uuid4().hex[:8]
    return f"sess_{ts}_{suffix}"


class SessionRepository:
    """Typed repository for session persistence.

    All public methods are async. Wraps SessionDatabase with structured
    return types and JSON serialization helpers.
    """

    def __init__(self, db_path: str | None = None) -> None:
        self._db = SessionDatabase(db_path)

    @property
    def db(self) -> SessionDatabase:
        return self._db

    # ── Lifecycle ───────────────────────────────────────────────────────────

    async def connect(self) -> None:
        """Open the database and ensure schema exists."""
        await self._db.connect()

    async def close(self) -> None:
        await self._db.close()

    async def __aenter__(self) -> SessionRepository:
        await self.connect()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()

    # ── Sessions ────────────────────────────────────────────────────────────

    async def create_session(
        self,
        title: str = "",
        status: str = "active",
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Create a new session. Returns the session dict."""
        sid = session_id or _make_session_id()
        return await self._db.create_session(sid, title=title, status=status)

    async def get_session(self, session_id: str) -> dict[str, Any] | None:
        """Fetch a session by ID, or None if not found."""
        return await self._db.get_session(session_id)

    async def list_sessions(
        self, limit: int = 50, offset: int = 0, status: str | None = None
    ) -> list[dict[str, Any]]:
        """List sessions, newest first. Optionally filter by status."""
        return await self._db.list_sessions(limit=limit, offset=offset, status=status)

    async def update_session(
        self, session_id: str, title: str | None = None, status: str | None = None
    ) -> dict[str, Any] | None:
        """Update session title and/or status. Returns updated session or None."""
        return await self._db.update_session(session_id, title=title, status=status)

    async def delete_session(self, session_id: str) -> bool:
        """Delete a session and all its messages/decisions/task_refs.

        Returns True if the session existed and was deleted.
        """
        return await self._db.delete_session(session_id)

    # ── Messages ────────────────────────────────────────────────────────────

    async def add_message(
        self,
        session_id: str,
        role: str,
        content: str,
        intent: str = "",
        correlation_id: str = "",
    ) -> dict[str, Any]:
        """Add a message to a session. Returns the message dict."""
        return await self._db.add_message(
            session_id,
            role=role,
            content=content,
            intent=intent,
            correlation_id=correlation_id,
        )

    async def get_messages(
        self,
        session_id: str,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Get messages for a session, oldest first."""
        return await self._db.get_messages(session_id, limit=limit, offset=offset)

    # ── Decisions ───────────────────────────────────────────────────────────

    async def add_decision(
        self,
        session_id: str,
        intent: str,
        confidence: float = 0.0,
        entities: dict[str, Any] | None = None,
        reason_code: str = "",
        response_mode: str = "",
    ) -> dict[str, Any]:
        """Record a router decision. ``entities`` is serialised to JSON internally."""
        return await self._db.add_decision(
            session_id,
            intent=intent,
            confidence=confidence,
            entities_json=json.dumps(entities or {}, ensure_ascii=False),
            reason_code=reason_code,
            response_mode=response_mode,
        )

    async def get_decisions(
        self,
        session_id: str,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Get decisions for a session, oldest first."""
        return await self._db.get_decisions(session_id, limit=limit, offset=offset)

    # ── Task refs ───────────────────────────────────────────────────────────

    async def add_task_ref(
        self,
        session_id: str,
        flow_id: str,
        tool_name: str = "",
        status: str = "created",
    ) -> dict[str, Any]:
        """Record a reference to a task flow created during this session."""
        return await self._db.add_task_ref(
            session_id, flow_id=flow_id, tool_name=tool_name, status=status
        )

    async def update_task_ref(self, ref_id: int, status: str) -> dict[str, Any] | None:
        """Update the status of a task ref by its integer ID."""
        return await self._db.update_task_ref(ref_id, status=status)

    async def get_task_refs(
        self, session_id: str, limit: int = 100, offset: int = 0
    ) -> list[dict[str, Any]]:
        """Get task refs for a session, oldest first."""
        return await self._db.get_task_refs(session_id, limit=limit, offset=offset)

    # ── Convenience ─────────────────────────────────────────────────────────

    async def session_exists(self, session_id: str) -> bool:
        """Quick check if a session exists (lightweight, no data load)."""
        return await self._db.session_exists(session_id)
