"""Durable Tool Ledger for Antigona.

Provides persistent SQLite-backed reservation and settlement for logical tool calls.
Ensures tool call idempotency survives process restarts.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from antigona.core import paths


class DurableToolLedger:
    """SQLite-backed persistent tool call ledger."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path = str(db_path or paths.database_path())
        self._ensure_table()

    def _get_conn(self) -> sqlite3.Connection:
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        """Yield a transaction connection and always close the native handle."""
        conn = self._get_conn()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _ensure_table(self) -> None:
        with self._connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS durable_tool_ledger (
                    call_hash TEXT PRIMARY KEY,
                    turn_id TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result TEXT,
                    error TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                """
            )
            conn.commit()

    def get(self, call_hash: str) -> dict[str, Any] | None:
        with self._connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT call_hash, turn_id, tool_name, status, result, error FROM durable_tool_ledger WHERE call_hash = ?",
                (call_hash,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return {
                "call_hash": row[0],
                "turn_id": row[1],
                "tool_name": row[2],
                "status": row[3],
                "result": row[4],
                "error": row[5],
            }

    def reserve(self, call_hash: str, turn_id: str, tool_name: str) -> tuple[str, dict[str, Any] | None]:
        """Atomically reserve a call_hash.

        Returns:
            ("RESERVED", None) if newly reserved.
            (existing_status, existing_row) if already present.
        """
        with self._connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT status, result, error FROM durable_tool_ledger WHERE call_hash = ?",
                (call_hash,),
            )
            row = cur.fetchone()
            if row:
                return row[0], {"status": row[0], "result": row[1], "error": row[2]}

            try:
                cur.execute(
                    "INSERT INTO durable_tool_ledger (call_hash, turn_id, tool_name, status) VALUES (?, ?, ?, 'PENDING')",
                    (call_hash, turn_id, tool_name),
                )
                conn.commit()
                return "RESERVED", None
            except sqlite3.IntegrityError:
                cur.execute(
                    "SELECT status, result, error FROM durable_tool_ledger WHERE call_hash = ?",
                    (call_hash,),
                )
                r = cur.fetchone()
                return (r[0] if r else "UNCERTAIN"), ({"status": r[0], "result": r[1], "error": r[2]} if r else None)

    def settle(self, call_hash: str, status: str, result: str | None = None, error: str | None = None) -> None:
        with self._connection() as conn:
            conn.execute(
                "UPDATE durable_tool_ledger SET status = ?, result = ?, error = ?, updated_at = CURRENT_TIMESTAMP WHERE call_hash = ?",
                (status, result, error, call_hash),
            )
            conn.commit()

    def clear(self, call_hash: str) -> None:
        with self._connection() as conn:
            conn.execute("DELETE FROM durable_tool_ledger WHERE call_hash = ?", (call_hash,))
            conn.commit()
