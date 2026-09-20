"""ApprovalGrant — the single canonical issuer/verifier of tool approvals.

An approval is not a string the caller invents: it is a grant issued by
:meth:`ApprovalGrantStore.issue` and bound to

* ``actor``      — who is allowed to use it,
* ``tool_name``  — which tool it authorizes,
* ``args_digest``— which exact arguments were approved,
* ``expires_at`` — for how long,
* ``one_shot``   — and how many times (once, by default).

Storage is SQLite (same shape as :mod:`antigona.durable.tool_ledger`) because
"consumed" and "expired" must survive a process restart — otherwise a replay
after restart re-authorizes the call — and because the issuer and the dispatch
path may live in different processes (gateway / worker / CLI).

Only the SHA-256 of the token is persisted; the raw token is returned to the
caller exactly once and never lands in the database or in the audit log. The
audit trail carries ``grant_id`` (``token_hash[:16]``) only.

Two verification surfaces exist and must never be confused:

* :meth:`ApprovalGrantStore.verify` / :meth:`ApprovalGrantStore.verify_and_consume`
  take the RAW token returned by :meth:`ApprovalGrantStore.issue`. The only key
  they ever look up is ``sha256(raw_token)``; presenting the persisted digest as
  if it were a token is refused with ``NOT_FOUND``.
* :meth:`ApprovalGrantStore.verify_stored` /
  :meth:`ApprovalGrantStore.verify_and_consume_stored` take the persisted primary
  key (the ``approval_grants.token_hash`` column) verbatim, never hashed. They
  exist only for the durable owner-approval path, where the raw token was
  deliberately not retained; every external surface must use the raw-token
  methods above.

Every verification failure is fail-closed: any internal error yields an invalid
verdict (``STORE_ERROR``), never an allow.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import sqlite3
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from antigona.core import paths

logger = logging.getLogger(__name__)

DEFAULT_TTL_SECONDS = 300


def compute_args_digest(tool_name: str, params: Mapping[str, Any]) -> str:
    """Digest the tool identity together with its canonical arguments.

    Underscore-prefixed keys are dispatch metadata (``_correlation_id``,
    ``_user_id``, ...), not tool arguments, so they are excluded — same rule as
    :func:`antigona.engine.unified_executor.compute_call_hash`.
    """
    clean = {k: v for k, v in params.items() if not str(k).startswith("_")}
    canonical = json.dumps(
        clean, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    )
    return hashlib.sha256(f"{tool_name}|{canonical}".encode()).hexdigest()


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


_HEX_DIGEST_CHARS = frozenset("0123456789abcdef")


def _is_persisted_key(value: object) -> bool:
    """True iff ``value`` is a 64-char lowercase hex digest.

    That is exactly the form :meth:`ApprovalGrantStore.issue` writes into the
    ``approval_grants.token_hash`` primary key. Anything else fails closed when
    presented to the stored-key surface.
    """
    if not isinstance(value, str):
        return False
    return len(value) == 64 and all(ch in _HEX_DIGEST_CHARS for ch in value)


class GrantDenialReason(StrEnum):
    """Why a grant was refused. Audit-only — never returned to the caller."""

    NOT_FOUND = "grant_not_found"
    EXPIRED = "grant_expired"
    CONSUMED = "grant_consumed"
    ACTOR_MISMATCH = "grant_actor_mismatch"
    TOOL_MISMATCH = "grant_tool_mismatch"
    ARGS_MISMATCH = "grant_args_mismatch"
    STORE_ERROR = "grant_store_error"


@dataclass(frozen=True)
class ApprovalGrant:
    """A persisted approval grant. ``token_hash`` is the primary key."""

    token_hash: str
    actor: str
    tool_name: str
    args_digest: str
    issuer: str
    issued_at: float
    expires_at: float
    one_shot: bool
    consumed_at: float | None = None
    consumed_by: str | None = None
    channel: str = ""
    session_id: str = ""
    reason: str = ""

    @property
    def grant_id(self) -> str:
        """Short, audit-safe identifier (never the raw token)."""
        return self.token_hash[:16]


@dataclass(frozen=True)
class GrantVerdict:
    """Outcome of a verification. ``grant_id`` is safe to write to the audit."""

    valid: bool
    reason: GrantDenialReason | None = None
    grant_id: str = ""


class ApprovalGrantStore:
    """SQLite-backed store of one-shot approval grants."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path = str(db_path or paths.database_path())
        # True when the target table carries a legacy ``request_id`` column
        # (a UNIQUE-indexed identifier present on at least one already-deployed
        # runtime schema). The canonical INSERT must then also populate it, or
        # the second grant collides on ``request_id = ''``.
        self._has_request_id: bool = False
        self._ensure_table()

    # ── Storage plumbing ───────────────────────────────────────────────────

    def _get_conn(self) -> sqlite3.Connection:
        parent = os.path.dirname(self.db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        """Yield a transactional connection and always close the native handle."""
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
                CREATE TABLE IF NOT EXISTS approval_grants (
                    token_hash TEXT PRIMARY KEY,
                    actor TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    args_digest TEXT NOT NULL,
                    issuer TEXT NOT NULL,
                    issued_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    one_shot INTEGER NOT NULL,
                    consumed_at REAL,
                    consumed_by TEXT,
                    channel TEXT NOT NULL DEFAULT '',
                    session_id TEXT NOT NULL DEFAULT '',
                    reason TEXT NOT NULL DEFAULT ''
                );
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_approval_grants_actor_tool "
                "ON approval_grants (actor, tool_name);"
            )
            columns = {
                str(row[1])
                for row in conn.execute("PRAGMA table_info(approval_grants)")
            }
        self._has_request_id = "request_id" in columns

    # ── Issuing ────────────────────────────────────────────────────────────

    def issue(
        self,
        *,
        actor: str,
        tool_name: str,
        args: Mapping[str, Any],
        issuer: str,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        one_shot: bool = True,
        channel: str = "",
        session_id: str = "",
        reason: str = "",
        now: float | None = None,
    ) -> str:
        """Issue a grant and return the RAW token — the only time it exists.

        This is the single canonical way an approval token comes into being.
        Currently only the 2-step confirmation path
        (``PolicyEngine.verify_confirmation``) issues grants here; OwnerGate, the
        CLI picker and Telegram are decision surfaces NOT yet wired to ``issue()``
        (NEXT-1B-C). Any future issuer must call this method — never mint its own
        token. Each grant is bound to actor + tool + exact args digest + expiry
        and is consumed exactly once.
        """
        issued_at = time.time() if now is None else now
        token = secrets.token_urlsafe(32)
        grant = ApprovalGrant(
            token_hash=_hash_token(token),
            actor=str(actor),
            tool_name=str(tool_name),
            args_digest=compute_args_digest(str(tool_name), args),
            issuer=str(issuer),
            issued_at=issued_at,
            expires_at=issued_at + float(ttl_seconds),
            one_shot=one_shot,
            channel=str(channel),
            session_id=str(session_id),
            reason=str(reason),
        )
        with self._connection() as conn:
            if self._has_request_id:
                # Legacy schema: the same canonical values, plus the grant's own
                # token hash as the (UNIQUE) legacy request identifier. No second
                # identifier is generated and no schema/rows/indexes are altered.
                conn.execute(
                    """
                    INSERT INTO approval_grants (
                        token_hash, actor, tool_name, args_digest, issuer,
                        issued_at, expires_at, one_shot, consumed_at,
                        consumed_by, channel, session_id, reason, request_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?)
                    """,
                    (
                        grant.token_hash,
                        grant.actor,
                        grant.tool_name,
                        grant.args_digest,
                        grant.issuer,
                        grant.issued_at,
                        grant.expires_at,
                        1 if grant.one_shot else 0,
                        grant.channel,
                        grant.session_id,
                        grant.reason,
                        grant.token_hash,
                    ),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO approval_grants (
                        token_hash, actor, tool_name, args_digest, issuer,
                        issued_at, expires_at, one_shot, consumed_at, consumed_by,
                        channel, session_id, reason
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?)
                    """,
                    (
                        grant.token_hash,
                        grant.actor,
                        grant.tool_name,
                        grant.args_digest,
                        grant.issuer,
                        grant.issued_at,
                        grant.expires_at,
                        1 if grant.one_shot else 0,
                        grant.channel,
                        grant.session_id,
                        grant.reason,
                    ),
                )
        logger.info(
            "Approval grant issued: grant_id=%s issuer=%s actor=%s tool=%s",
            grant.grant_id,
            grant.issuer,
            grant.actor,
            grant.tool_name,
        )
        return token

    # ── Verification ───────────────────────────────────────────────────────

    def _load(self, conn: sqlite3.Connection, token_hash: str) -> ApprovalGrant | None:
        cur = conn.execute(
            """
            SELECT token_hash, actor, tool_name, args_digest, issuer, issued_at,
                   expires_at, one_shot, consumed_at, consumed_by, channel,
                   session_id, reason
            FROM approval_grants WHERE token_hash = ?
            """,
            (token_hash,),
        )
        row: tuple[Any, ...] | None = cur.fetchone()
        if row is None:
            return None
        return ApprovalGrant(
            token_hash=str(row[0]),
            actor=str(row[1]),
            tool_name=str(row[2]),
            args_digest=str(row[3]),
            issuer=str(row[4]),
            issued_at=float(row[5]),
            expires_at=float(row[6]),
            one_shot=bool(row[7]),
            consumed_at=None if row[8] is None else float(row[8]),
            consumed_by=None if row[9] is None else str(row[9]),
            channel=str(row[10]),
            session_id=str(row[11]),
            reason=str(row[12]),
        )

    def _check_binding(
        self,
        grant: ApprovalGrant,
        *,
        actor: str,
        tool_name: str,
        args: Mapping[str, Any],
        now: float,
    ) -> GrantDenialReason | None:
        """Return the first violated binding, or None when the grant matches."""
        if grant.expires_at <= now:
            return GrantDenialReason.EXPIRED
        if grant.consumed_at is not None:
            return GrantDenialReason.CONSUMED
        if grant.actor != str(actor):
            return GrantDenialReason.ACTOR_MISMATCH
        if grant.tool_name != str(tool_name):
            return GrantDenialReason.TOOL_MISMATCH
        if grant.args_digest != compute_args_digest(str(tool_name), args):
            return GrantDenialReason.ARGS_MISMATCH
        return None

    def _verify_key(
        self,
        token_hash: str,
        *,
        actor: str,
        tool_name: str,
        args: Mapping[str, Any],
        now: float,
    ) -> GrantVerdict:
        """Look ``token_hash`` up verbatim and check its bindings (no consume)."""
        try:
            with self._connection() as conn:
                grant = self._load(conn, token_hash)
        except Exception:
            logger.exception("Approval grant lookup failed (fail-closed)")
            return GrantVerdict(valid=False, reason=GrantDenialReason.STORE_ERROR)
        if grant is None:
            return GrantVerdict(valid=False, reason=GrantDenialReason.NOT_FOUND)
        violation = self._check_binding(
            grant, actor=actor, tool_name=tool_name, args=args, now=now
        )
        if violation is not None:
            return GrantVerdict(valid=False, reason=violation, grant_id=grant.grant_id)
        return GrantVerdict(valid=True, grant_id=grant.grant_id)

    def _consume_key(
        self,
        token_hash: str,
        *,
        actor: str,
        tool_name: str,
        args: Mapping[str, Any],
        consumed_by: str,
        now: float,
    ) -> GrantVerdict:
        """Look ``token_hash`` up verbatim, check bindings and consume (CAS)."""
        try:
            with self._connection() as conn:
                grant = self._load(conn, token_hash)
                if grant is None:
                    return GrantVerdict(valid=False, reason=GrantDenialReason.NOT_FOUND)
                violation = self._check_binding(
                    grant, actor=actor, tool_name=tool_name, args=args, now=now
                )
                if violation is not None:
                    return GrantVerdict(
                        valid=False, reason=violation, grant_id=grant.grant_id
                    )
                if not grant.one_shot:
                    return GrantVerdict(valid=True, grant_id=grant.grant_id)
                cur = conn.execute(
                    "UPDATE approval_grants SET consumed_at = ?, consumed_by = ? "
                    "WHERE token_hash = ? AND consumed_at IS NULL",
                    (now, str(consumed_by), token_hash),
                )
                if cur.rowcount != 1:
                    # Lost the race: another dispatch consumed this grant.
                    return GrantVerdict(
                        valid=False,
                        reason=GrantDenialReason.CONSUMED,
                        grant_id=grant.grant_id,
                    )
                return GrantVerdict(valid=True, grant_id=grant.grant_id)
        except Exception:
            logger.exception("Approval grant consumption failed (fail-closed)")
            return GrantVerdict(valid=False, reason=GrantDenialReason.STORE_ERROR)

    def verify(
        self,
        token: str,
        *,
        actor: str,
        tool_name: str,
        args: Mapping[str, Any],
        now: float | None = None,
    ) -> GrantVerdict:
        """Check a RAW grant token without consuming it.

        ``token`` is the token returned by :meth:`issue`. The only key looked up
        is ``sha256(token)``; the persisted digest of a grant is NOT a token and
        yields ``NOT_FOUND``.
        """
        checked_at = time.time() if now is None else now
        if not token.strip():
            return GrantVerdict(valid=False, reason=GrantDenialReason.NOT_FOUND)
        return self._verify_key(
            _hash_token(token.strip()),
            actor=actor,
            tool_name=tool_name,
            args=args,
            now=checked_at,
        )

    def verify_stored(
        self,
        token_hash: str,
        *,
        actor: str,
        tool_name: str,
        args: Mapping[str, Any],
        now: float | None = None,
    ) -> GrantVerdict:
        """Check a grant by its PERSISTED primary key (looked up verbatim).

        ``token_hash`` is the persisted primary key — the value of the
        ``approval_grants.token_hash`` column as written by :meth:`issue` — and
        is looked up verbatim, never hashed. It is the persisted primary key;
        ONLY the durable owner-approval path (where the raw token was
        deliberately not retained) may use this; every external surface must
        call :meth:`verify` with the RAW token. Anything that is not a 64-char
        lowercase hex digest fails closed with ``NOT_FOUND``, never an
        exception.
        """
        checked_at = time.time() if now is None else now
        if not _is_persisted_key(token_hash):
            return GrantVerdict(valid=False, reason=GrantDenialReason.NOT_FOUND)
        return self._verify_key(
            token_hash,
            actor=actor,
            tool_name=tool_name,
            args=args,
            now=checked_at,
        )

    def verify_and_consume(
        self,
        token: str,
        *,
        actor: str,
        tool_name: str,
        args: Mapping[str, Any],
        consumed_by: str = "",
        now: float | None = None,
    ) -> GrantVerdict:
        """Check a RAW grant token and atomically consume it when one-shot.

        ``token`` is the token returned by :meth:`issue`. The only key looked up
        is ``sha256(token)``; the persisted digest of a grant is NOT a token and
        yields ``NOT_FOUND``.

        The consumption is a compare-and-set (``WHERE consumed_at IS NULL``)
        so two concurrent dispatches of the same token yield exactly one
        execution — the same technique as ``OwnerGate.decide()``.
        """
        checked_at = time.time() if now is None else now
        if not token.strip():
            return GrantVerdict(valid=False, reason=GrantDenialReason.NOT_FOUND)
        return self._consume_key(
            _hash_token(token.strip()),
            actor=actor,
            tool_name=tool_name,
            args=args,
            consumed_by=consumed_by,
            now=checked_at,
        )

    def verify_and_consume_stored(
        self,
        token_hash: str,
        *,
        actor: str,
        tool_name: str,
        args: Mapping[str, Any],
        consumed_by: str = "",
        now: float | None = None,
    ) -> GrantVerdict:
        """Check-and-consume a grant by its PERSISTED primary key (verbatim).

        ``token_hash`` is the persisted primary key — the value of the
        ``approval_grants.token_hash`` column as written by :meth:`issue` — and
        is looked up verbatim, never hashed. It is the persisted primary key;
        ONLY the durable owner-approval path (where the raw token was
        deliberately not retained) may use this; every external surface must
        call :meth:`verify_and_consume` with the RAW token. Anything that is not
        a 64-char lowercase hex digest fails closed with ``NOT_FOUND``, never an
        exception.
        """
        checked_at = time.time() if now is None else now
        if not _is_persisted_key(token_hash):
            return GrantVerdict(valid=False, reason=GrantDenialReason.NOT_FOUND)
        return self._consume_key(
            token_hash,
            actor=actor,
            tool_name=tool_name,
            args=args,
            consumed_by=consumed_by,
            now=checked_at,
        )

    # ── Housekeeping ───────────────────────────────────────────────────────

    def purge_expired(self, *, now: float | None = None) -> int:
        """Delete expired grants. Returns the number of rows removed."""
        cutoff = time.time() if now is None else now
        try:
            with self._connection() as conn:
                cur = conn.execute(
                    "DELETE FROM approval_grants WHERE expires_at <= ?", (cutoff,)
                )
                return int(cur.rowcount)
        except Exception:
            logger.exception("Approval grant purge failed")
            return 0
