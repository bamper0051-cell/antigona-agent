"""Durable exactly-once ledger for Telegram → runtime turns.

Telegram redelivers updates and the bot process can restart mid-turn, so
process-local dedup alone cannot promise "exactly one agent run per message".
This ledger persists one row per canonical turn key *before* the Gateway call
and settles it afterwards, which gives three guarantees:

* a redelivered update whose turn already completed replays the stored payload
  and never reaches the Gateway again;
* a turn that failed before the Gateway accepted it stays retryable;
* a turn left in flight by a *previous* process is reported as ambiguous and is
  never re-invoked, because the runtime may already have executed it.

Storage is stdlib ``sqlite3`` (no new dependency) executed on a worker thread.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

logger = logging.getLogger(__name__)

#: Rows older than this are pruned opportunistically.  Telegram itself stops
#: redelivering an update long before this window elapses.
DEFAULT_RETENTION_SECONDS: Final = 7 * 24 * 3600

_SCHEMA: Final = """
CREATE TABLE IF NOT EXISTS telegram_turns (
    turn_key    TEXT PRIMARY KEY,
    chat_id     INTEGER NOT NULL,
    state       TEXT NOT NULL,
    owner_token TEXT NOT NULL,
    payload     TEXT,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_telegram_turns_chat ON telegram_turns (chat_id);
CREATE INDEX IF NOT EXISTS ix_telegram_turns_updated ON telegram_turns (updated_at);

CREATE TABLE IF NOT EXISTS telegram_queue (
    queue_id             TEXT PRIMARY KEY,
    chat_id              INTEGER NOT NULL,
    message_id           INTEGER NOT NULL,
    task_tag             TEXT,
    text                 TEXT NOT NULL,
    attachment_ids       TEXT NOT NULL,
    received_at          REAL NOT NULL,
    status               TEXT NOT NULL,
    reply_to_message_id  INTEGER,
    updated_at           REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_telegram_queue_chat ON telegram_queue (chat_id);
CREATE INDEX IF NOT EXISTS ix_telegram_queue_status ON telegram_queue (status);
"""


def default_ledger_path() -> Path:
    """Governed durable-ledger location (see :func:`antigona.core.paths.turn_ledger_path`)."""
    from antigona.core import paths

    return paths.turn_ledger_path()


def ensure_ledger_writable(path: Path | str) -> Path:
    """Fail closed unless *path* can serve as a durable exactly-once ledger.

    The Telegram exactly-once promise survives a restart only if the ledger is
    on durable, writable storage. An unwritable ledger must surface as a hard,
    actionable error — never a silent "continuing without recovery" that
    re-invokes (or drops) a turn the runtime may already have executed.
    """
    resolved = Path(path)
    try:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            resolved, isolation_level=None, check_same_thread=False
        )
    except (OSError, sqlite3.Error) as exc:
        raise RuntimeError(
            f"durable Telegram turn ledger is not writable at {resolved}: {exc}. "
            "In an immutable deployment set ANTIGONA_STATE_ROOT (or "
            "ANTIGONA_TELEGRAM_TURN_LEDGER) to a writable path outside the "
            "read-only code root; refusing to run without exactly-once recovery."
        ) from exc
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.executescript(_SCHEMA)
    except (OSError, sqlite3.Error) as exc:
        raise RuntimeError(
            f"durable Telegram turn ledger at {resolved} is unreadable/unusable: "
            f"{exc}; refusing to run without exactly-once recovery."
        ) from exc
    finally:
        connection.close()
    return resolved


class TurnState(StrEnum):
    IN_FLIGHT = "IN_FLIGHT"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class QueueStatus(StrEnum):
    """Lifecycle of one inbound Telegram message through the bridge's FIFO.

    Distinct from :class:`TurnState`: ``TurnState`` is the exactly-once
    execution ledger (has the Gateway been invoked?); ``QueueStatus`` is the
    transport-queue contract field the PC/Telegram slice exposes (where is
    this message in the per-chat FIFO right now?).
    """

    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    DROPPED_DUPLICATE = "dropped_duplicate"


class ClaimOutcome(StrEnum):
    #: This process now owns the turn and must call the Gateway exactly once.
    CLAIMED = "CLAIMED"
    #: The turn already completed; replay the stored payload.
    REPLAY = "REPLAY"
    #: This process is already running the turn (in-memory future attaches).
    IN_FLIGHT_LOCAL = "IN_FLIGHT_LOCAL"
    #: A previous process left the turn in flight — re-invocation is unsafe.
    AMBIGUOUS = "AMBIGUOUS"


@dataclass(frozen=True, slots=True)
class TurnClaim:
    outcome: ClaimOutcome
    payload: dict[str, Any] | None = None


class AmbiguousTurn(RuntimeError):
    """A turn from a previous process may or may not have run."""


class TurnLedger:
    """Durable turn registry keyed by the canonical Telegram turn key."""

    def __init__(
        self,
        path: Path | str,
        *,
        owner_token: str | None = None,
        retention_seconds: float = DEFAULT_RETENTION_SECONDS,
    ) -> None:
        self._path = Path(path)
        self._owner_token = owner_token or uuid.uuid4().hex
        self._retention = max(60.0, float(retention_seconds))
        self._connection: sqlite3.Connection | None = None
        self._io_lock = asyncio.Lock()
        self._closed = False

    @property
    def owner_token(self) -> str:
        return self._owner_token

    # ── connection ───────────────────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        if self._connection is not None:
            return self._connection
        self._path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            self._path,
            isolation_level=None,  # explicit BEGIN IMMEDIATE below
            check_same_thread=False,
        )
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.executescript(_SCHEMA)
        self._connection = connection
        return connection

    async def _run(self, work: Any) -> Any:
        # One writer at a time: the ledger is the serialisation point for
        # exactly-once, so concurrent claims must not interleave.
        async with self._io_lock:
            if self._closed:
                raise RuntimeError("turn ledger is closed")
            return await asyncio.to_thread(work)

    # ── operations ───────────────────────────────────────────────────────

    async def claim(self, turn_key: str, chat_id: int) -> TurnClaim:
        """Atomically decide whether this process may invoke the runtime."""

        def _work() -> TurnClaim:
            connection = self._connect()
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT state, owner_token, payload FROM telegram_turns "
                    "WHERE turn_key = ?",
                    (turn_key,),
                ).fetchone()
                now = time.time()
                if row is None:
                    connection.execute(
                        "INSERT INTO telegram_turns "
                        "(turn_key, chat_id, state, owner_token, payload, "
                        " created_at, updated_at) VALUES (?, ?, ?, ?, NULL, ?, ?)",
                        (
                            turn_key,
                            chat_id,
                            TurnState.IN_FLIGHT.value,
                            self._owner_token,
                            now,
                            now,
                        ),
                    )
                    connection.execute("COMMIT")
                    return TurnClaim(ClaimOutcome.CLAIMED)

                state, owner_token, payload = row
                if state == TurnState.COMPLETED.value:
                    connection.execute("COMMIT")
                    decoded = json.loads(payload) if payload else {}
                    return TurnClaim(ClaimOutcome.REPLAY, decoded)
                if state == TurnState.FAILED.value:
                    # Nothing was accepted by the runtime, so a Telegram retry
                    # is allowed to submit again under this process's token.
                    connection.execute(
                        "UPDATE telegram_turns SET state = ?, owner_token = ?, "
                        "updated_at = ? WHERE turn_key = ?",
                        (
                            TurnState.IN_FLIGHT.value,
                            self._owner_token,
                            now,
                            turn_key,
                        ),
                    )
                    connection.execute("COMMIT")
                    return TurnClaim(ClaimOutcome.CLAIMED)
                connection.execute("COMMIT")
                if owner_token == self._owner_token:
                    return TurnClaim(ClaimOutcome.IN_FLIGHT_LOCAL)
                return TurnClaim(ClaimOutcome.AMBIGUOUS)
            except BaseException:
                connection.execute("ROLLBACK")
                raise

        claim: TurnClaim = await self._run(_work)
        return claim

    async def complete(self, turn_key: str, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, ensure_ascii=False, default=str)

        def _work() -> None:
            connection = self._connect()
            connection.execute(
                "UPDATE telegram_turns SET state = ?, payload = ?, updated_at = ? "
                "WHERE turn_key = ?",
                (TurnState.COMPLETED.value, encoded, time.time(), turn_key),
            )

        await self._run(_work)

    async def fail(self, turn_key: str) -> None:
        """Mark an unaccepted turn retryable without poisoning dedup."""

        def _work() -> None:
            connection = self._connect()
            connection.execute(
                "UPDATE telegram_turns SET state = ?, updated_at = ? "
                "WHERE turn_key = ? AND state = ?",
                (
                    TurnState.FAILED.value,
                    time.time(),
                    turn_key,
                    TurnState.IN_FLIGHT.value,
                ),
            )

        await self._run(_work)

    async def state_of(self, turn_key: str) -> TurnState | None:
        def _work() -> TurnState | None:
            connection = self._connect()
            row = connection.execute(
                "SELECT state FROM telegram_turns WHERE turn_key = ?",
                (turn_key,),
            ).fetchone()
            return TurnState(row[0]) if row else None

        state: TurnState | None = await self._run(_work)
        return state

    async def prune(self) -> int:
        cutoff = time.time() - self._retention

        def _work() -> int:
            connection = self._connect()
            cursor = connection.execute(
                "DELETE FROM telegram_turns WHERE updated_at < ? AND state != ?",
                (cutoff, TurnState.IN_FLIGHT.value),
            )
            removed = int(cursor.rowcount or 0)
            terminal = (
                QueueStatus.DONE.value,
                QueueStatus.FAILED.value,
                QueueStatus.DROPPED_DUPLICATE.value,
            )
            queue_cursor = connection.execute(
                "DELETE FROM telegram_queue WHERE updated_at < ? AND status IN "
                "(?, ?, ?)",
                (cutoff, *terminal),
            )
            removed += int(queue_cursor.rowcount or 0)
            return removed

        removed: int = await self._run(_work)
        return removed

    # ── Queue contract (observability of the per-chat FIFO) ───────────────

    async def queue_enqueue(
        self,
        *,
        queue_id: str,
        chat_id: int,
        message_id: int,
        task_tag: str | None,
        text: str,
        attachment_ids: Sequence[str],
        reply_to_message_id: int | None,
        status: QueueStatus,
    ) -> None:
        """Record one inbound message entering the per-chat FIFO.

        Idempotent by ``queue_id`` (``INSERT OR REPLACE``): a caller that
        retries the same queue entry (e.g. the REPLAY path settling straight
        to a terminal status) never produces a duplicate row.
        """
        now = time.time()
        encoded_attachments = json.dumps(list(attachment_ids), ensure_ascii=False)

        def _work() -> None:
            connection = self._connect()
            connection.execute(
                "INSERT OR REPLACE INTO telegram_queue "
                "(queue_id, chat_id, message_id, task_tag, text, attachment_ids, "
                " received_at, status, reply_to_message_id, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    queue_id,
                    chat_id,
                    message_id,
                    task_tag,
                    text,
                    encoded_attachments,
                    now,
                    status.value,
                    reply_to_message_id,
                    now,
                ),
            )

        await self._run(_work)

    async def queue_set_status(self, queue_id: str, status: QueueStatus) -> None:
        def _work() -> None:
            connection = self._connect()
            connection.execute(
                "UPDATE telegram_queue SET status = ?, updated_at = ? "
                "WHERE queue_id = ?",
                (status.value, time.time(), queue_id),
            )

        await self._run(_work)

    async def queue_fetch_pending(self, chat_id: int | None = None) -> list[dict[str, Any]]:
        """Fetch pending queued items for restart recovery, ordered by received_at."""
        def _work() -> list[dict[str, Any]]:
            connection = self._connect()
            connection.row_factory = sqlite3.Row
            if chat_id is not None:
                rows = connection.execute(
                    "SELECT queue_id, chat_id, message_id, task_tag, text, "
                    "attachment_ids, received_at, status, reply_to_message_id, updated_at "
                    "FROM telegram_queue WHERE chat_id = ? AND status IN (?, ?) "
                    "ORDER BY received_at ASC",
                    (chat_id, QueueStatus.QUEUED.value, QueueStatus.RUNNING.value),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT queue_id, chat_id, message_id, task_tag, text, "
                    "attachment_ids, received_at, status, reply_to_message_id, updated_at "
                    "FROM telegram_queue WHERE status IN (?, ?) "
                    "ORDER BY received_at ASC",
                    (QueueStatus.QUEUED.value, QueueStatus.RUNNING.value),
                ).fetchall()
            return [dict(r) for r in rows]

        pending: list[dict[str, Any]] = await self._run(_work)
        return pending

    async def queue_get(self, queue_id: str) -> dict[str, Any] | None:
        def _work() -> dict[str, Any] | None:
            connection = self._connect()
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT queue_id, chat_id, message_id, task_tag, text, "
                "attachment_ids, received_at, status, reply_to_message_id, updated_at "
                "FROM telegram_queue WHERE queue_id = ?",
                (queue_id,),
            ).fetchone()
            return dict(row) if row else None

        result: dict[str, Any] | None = await self._run(_work)
        return result

    async def reclaim_orphaned_turns(self, lease_timeout: float = 60.0) -> int:
        """Reset stale IN_FLIGHT turns from dead processes back to retryable FAILED state."""
        cutoff = time.time() - lease_timeout

        def _work() -> int:
            connection = self._connect()
            connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = connection.execute(
                    "UPDATE telegram_turns SET state = ?, updated_at = ? "
                    "WHERE state = ? AND updated_at < ? AND owner_token != ?",
                    (
                        TurnState.FAILED.value,
                        time.time(),
                        TurnState.IN_FLIGHT.value,
                        cutoff,
                        self._owner_token,
                    ),
                )
                reclaimed = int(cursor.rowcount or 0)
                connection.execute("COMMIT")
                return reclaimed
            except BaseException:
                connection.execute("ROLLBACK")
                raise

        reclaimed: int = await self._run(_work)
        return reclaimed

    async def close(self) -> None:
        async with self._io_lock:
            if self._closed:
                return
            self._closed = True
            connection = self._connection
            self._connection = None
        if connection is not None:
            await asyncio.to_thread(connection.close)


class InMemoryTurnLedger(TurnLedger):
    """Ledger without durability — used when no ledger path is configured.

    Exactly-once still holds for the life of the process; a restart simply
    forgets history instead of reporting ambiguity.
    """

    def __init__(self, *, owner_token: str | None = None) -> None:
        super().__init__(":memory:", owner_token=owner_token)

    def _connect(self) -> sqlite3.Connection:
        if self._connection is not None:
            return self._connection
        connection = sqlite3.connect(":memory:", isolation_level=None, check_same_thread=False)
        connection.executescript(_SCHEMA)
        self._connection = connection
        return connection
