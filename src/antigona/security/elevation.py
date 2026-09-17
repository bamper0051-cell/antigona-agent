"""ElevationAuthority — the single, durable store for owner elevation state.

Before this module (campaign finding CP-2) three modules each kept their own
in-RAM elevation + brute-force-lockout state:

* ``tools.pin_gate``     — ``_attempt_tracker`` + ``_sessions`` + ``_verified_sessions``
* ``security.owner_override`` — ``_failed_attempts`` / ``_lockout_until`` + ``_active_sessions``
* ``cli_ui.layout``      — ``owner_mode`` / ``pin_attempts``

Three lockout counters, four elevation flags, none of them durable — a process
restart wiped the brute-force lockout (measured, ``tests/characterization``).

This class owns that state once, keyed by an opaque ``principal`` string the
caller composes (``"telegram:chat:123"``, ``"owner:456"``, ``"cli:owner"``).
It is SQLite-backed (same lazy ``CREATE TABLE IF NOT EXISTS`` pattern as
:mod:`antigona.security.approval_grant`) so lockout and elevation survive a
restart, and a RAM cache keeps the hot path fast.

PIN *verification* (hashing, the secret) stays in the caller — ``pin_gate`` and
``OwnerOverrideManager`` still own their hash schemes. This module owns only the
*consequences*: how many failures, is it locked, is a session elevated.

Fail-closed: any storage error yields the safe answer — ``is_locked_out`` →
``True``, ``is_elevated`` → ``False``.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from antigona.core import paths

logger = logging.getLogger(__name__)

DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_LOCKOUT_SECONDS = 900.0
DEFAULT_SESSION_TTL_SECONDS = 900.0

_GLOBAL_LOCK = threading.RLock()


class ElevationAuthority:
    """One durable owner-elevation + brute-force-lockout store."""

    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        lockout_seconds: float = DEFAULT_LOCKOUT_SECONDS,
        session_ttl_seconds: float = DEFAULT_SESSION_TTL_SECONDS,
    ) -> None:
        self.db_path = str(db_path or paths.database_path())
        self.max_attempts = int(max_attempts)
        self.lockout_seconds = float(lockout_seconds)
        self.session_ttl_seconds = float(session_ttl_seconds)
        self._ensure_tables()

    # ── storage plumbing ──────────────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        parent = os.path.dirname(self.db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _ensure_tables(self) -> None:
        with self._tx() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS elevation_lockout (
                    principal TEXT PRIMARY KEY,
                    failed_attempts INTEGER NOT NULL DEFAULT 0,
                    first_failure_at REAL,
                    locked_until REAL NOT NULL DEFAULT 0
                );
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS elevation_session (
                    principal TEXT PRIMARY KEY,
                    elevated_at REAL NOT NULL,
                    expires_at REAL NOT NULL
                );
                """
            )

    # ── brute-force lockout ───────────────────────────────────────────────

    def is_locked_out(self, principal: str, *, now: float | None = None) -> bool:
        ts = time.time() if now is None else now
        try:
            with _GLOBAL_LOCK, self._tx() as conn:
                row = conn.execute(
                    "SELECT locked_until FROM elevation_lockout WHERE principal = ?",
                    (principal,),
                ).fetchone()
                if row is None:
                    return False
                locked_until = float(row[0] or 0.0)
                if locked_until and ts >= locked_until:
                    conn.execute(
                        "UPDATE elevation_lockout SET failed_attempts = 0, "
                        "first_failure_at = NULL, locked_until = 0 WHERE principal = ?",
                        (principal,),
                    )
                    return False
                return bool(locked_until) and ts < locked_until
        except Exception:
            logger.exception("ElevationAuthority.is_locked_out failed (fail-closed → locked)")
            return True

    def record_failure(self, principal: str, *, now: float | None = None) -> tuple[bool, int]:
        """Register one wrong PIN. Returns ``(locked_now, remaining_attempts)``."""
        ts = time.time() if now is None else now
        try:
            with _GLOBAL_LOCK, self._tx() as conn:
                row = conn.execute(
                    "SELECT failed_attempts, first_failure_at, locked_until "
                    "FROM elevation_lockout WHERE principal = ?",
                    (principal,),
                ).fetchone()
                attempts = int(row[0]) if row else 0
                first_at = (row[1] if row else None) or ts
                locked_until = float(row[2]) if row else 0.0
                # window elapsed since first failure → start a fresh streak
                if locked_until and ts >= locked_until:
                    attempts, first_at, locked_until = 0, ts, 0.0
                attempts += 1
                if attempts >= self.max_attempts:
                    locked_until = ts + self.lockout_seconds
                conn.execute(
                    "INSERT INTO elevation_lockout (principal, failed_attempts, "
                    "first_failure_at, locked_until) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(principal) DO UPDATE SET failed_attempts = excluded.failed_attempts, "
                    "first_failure_at = excluded.first_failure_at, locked_until = excluded.locked_until",
                    (principal, attempts, first_at, locked_until),
                )
                remaining = max(0, self.max_attempts - attempts)
                return bool(locked_until and ts < locked_until), remaining
        except Exception:
            logger.exception("ElevationAuthority.record_failure failed (fail-closed → locked)")
            return True, 0

    def record_success(self, principal: str) -> None:
        try:
            with _GLOBAL_LOCK, self._tx() as conn:
                conn.execute("DELETE FROM elevation_lockout WHERE principal = ?", (principal,))
        except Exception:
            logger.exception("ElevationAuthority.record_success failed")

    def failed_attempts(self, principal: str) -> int:
        try:
            with _GLOBAL_LOCK, self._tx() as conn:
                row = conn.execute(
                    "SELECT failed_attempts FROM elevation_lockout WHERE principal = ?",
                    (principal,),
                ).fetchone()
                return int(row[0]) if row else 0
        except Exception:
            return self.max_attempts  # fail-closed: assume exhausted

    def remaining_lockout(self, principal: str, *, now: float | None = None) -> float:
        ts = time.time() if now is None else now
        try:
            with _GLOBAL_LOCK, self._tx() as conn:
                row = conn.execute(
                    "SELECT locked_until FROM elevation_lockout WHERE principal = ?",
                    (principal,),
                ).fetchone()
                if not row:
                    return 0.0
                return max(0.0, float(row[0] or 0.0) - ts)
        except Exception:
            return self.lockout_seconds

    def reset(self, principal: str) -> None:
        self.record_success(principal)

    # ── elevated sessions ─────────────────────────────────────────────────

    def elevate(self, principal: str, *, ttl: float | None = None, now: float | None = None) -> None:
        ts = time.time() if now is None else now
        duration = self.session_ttl_seconds if ttl is None else float(ttl)
        try:
            with _GLOBAL_LOCK, self._tx() as conn:
                conn.execute(
                    "INSERT INTO elevation_session (principal, elevated_at, expires_at) "
                    "VALUES (?, ?, ?) ON CONFLICT(principal) DO UPDATE SET "
                    "elevated_at = excluded.elevated_at, expires_at = excluded.expires_at",
                    (principal, ts, ts + duration),
                )
                # a fresh elevation clears any brute-force state
                conn.execute("DELETE FROM elevation_lockout WHERE principal = ?", (principal,))
        except Exception:
            logger.exception("ElevationAuthority.elevate failed")

    def is_elevated(self, principal: str, *, now: float | None = None) -> bool:
        ts = time.time() if now is None else now
        try:
            with _GLOBAL_LOCK, self._tx() as conn:
                row = conn.execute(
                    "SELECT expires_at FROM elevation_session WHERE principal = ?",
                    (principal,),
                ).fetchone()
                if row is None:
                    return False
                if ts >= float(row[0]):
                    conn.execute(
                        "DELETE FROM elevation_session WHERE principal = ?", (principal,)
                    )
                    return False
                return True
        except Exception:
            logger.exception("ElevationAuthority.is_elevated failed (fail-closed → not elevated)")
            return False

    def remaining_session(self, principal: str, *, now: float | None = None) -> float:
        ts = time.time() if now is None else now
        try:
            with _GLOBAL_LOCK, self._tx() as conn:
                row = conn.execute(
                    "SELECT expires_at FROM elevation_session WHERE principal = ?",
                    (principal,),
                ).fetchone()
                return max(0.0, float(row[0]) - ts) if row else 0.0
        except Exception:
            return 0.0

    def session_meta(self, principal: str) -> tuple[float, float] | None:
        """``(elevated_at, expires_at)`` for a live-or-not row, or None. No expiry sweep."""
        try:
            with _GLOBAL_LOCK, self._tx() as conn:
                row = conn.execute(
                    "SELECT elevated_at, expires_at FROM elevation_session WHERE principal = ?",
                    (principal,),
                ).fetchone()
                return (float(row[0]), float(row[1])) if row else None
        except Exception:
            return None

    def revoke(self, principal: str) -> bool:
        try:
            with _GLOBAL_LOCK, self._tx() as conn:
                cur = conn.execute(
                    "DELETE FROM elevation_session WHERE principal = ?", (principal,)
                )
                return bool(cur.rowcount)
        except Exception:
            logger.exception("ElevationAuthority.revoke failed")
            return False

    def revoke_all(self) -> int:
        try:
            with _GLOBAL_LOCK, self._tx() as conn:
                cur = conn.execute("DELETE FROM elevation_session")
                return int(cur.rowcount or 0)
        except Exception:
            logger.exception("ElevationAuthority.revoke_all failed")
            return 0

    def clear_all(self) -> None:
        """Wipe every session AND every lockout row. Admin / config-change reset
        only — NOT a restart path (durable lockout must survive a restart)."""
        try:
            with _GLOBAL_LOCK, self._tx() as conn:
                conn.execute("DELETE FROM elevation_session")
                conn.execute("DELETE FROM elevation_lockout")
        except Exception:
            logger.exception("ElevationAuthority.clear_all failed")


#: Principal key for the interactive CLI's owner-mode elevation.
CLI_OWNER_PRINCIPAL = "cli:owner"


def principal_for(channel: str, user_id: str | int, session_id: str) -> str:
    """Canonical elevation-session key for a ``(channel, user_id, session_id)`` triple.

    The one key shape a policy check and a ``verify_and_elevate`` must agree on so
    an unlock on one is visible to the other (campaign CP-7).
    """
    return f"{channel}:{user_id}:{session_id}"


def owner_elevation_authority() -> ElevationAuthority:
    """The shared owner-elevation store: ``<owner_dir>/elevation.db``.

    Both :class:`~antigona.security.owner_override.OwnerOverrideManager` (via its
    default ``pin_file_path`` parent) and the interactive CLI PIN gate resolve to
    this same file, so ``/lock`` in one surface is visible to the other.
    """
    return ElevationAuthority(db_path=paths.elevation_db())
