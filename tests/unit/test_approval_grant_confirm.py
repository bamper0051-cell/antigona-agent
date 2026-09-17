"""Milestone 0 (P0 Security) — ApprovalGrant + confirm_and_continue live cycle.

Proves the canonical REQUEST → POLICY → APPROVAL → RE-DISPATCH flow works in
the live PolicyEngine with a DURABLE one-shot grant:

* REQUEST/POLICY: policy.check(CRITICAL) => allowed=False, requires_2step,
  pending (no durable grant yet).
* APPROVAL: verify_confirmation(owner phrase) mints a durable one-shot grant
  bound to actor+tool+args (persisted, restart-safe).
* RE-DISPATCH: the grant is consumed exactly once. Any replay / wrong binding
  => DENIED (fail-closed). confirm_and_continue() bundles APPROVAL+RE-DISPATCH
  for a surface-agnostic single call.
"""
import sqlite3
from pathlib import Path

import pytest

from antigona.policy.engine import PolicyEngine
from antigona.security.approval_grant import ApprovalGrantStore

CTX = {"channel": "cli", "user_id": "owner", "session_id": "s1"}


def _create_legacy_request_id_schema(db_path: Path) -> None:
    """The mixed schema already deployed on at least one runtime: every canonical
    column plus a legacy UNIQUE ``request_id`` the current canonical INSERT omits.
    """
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE approval_grants (
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
                reason TEXT NOT NULL DEFAULT '',
                request_id TEXT NOT NULL DEFAULT ''
            );
            CREATE UNIQUE INDEX idx_approval_grants_request_id
                ON approval_grants (request_id);
            """
        )


def test_issue_supports_legacy_unique_request_id_schema(tmp_path):
    """Two distinct grants must both persist against a legacy UNIQUE-``request_id``
    table. Base behaviour fails the second ``issue()`` with
    ``sqlite3.IntegrityError: UNIQUE constraint failed: approval_grants.request_id``
    because the canonical INSERT leaves ``request_id`` at its ``''`` default.
    """
    db_path = tmp_path / "legacy_grants.sqlite"
    _create_legacy_request_id_schema(db_path)
    store = ApprovalGrantStore(db_path=str(db_path))

    token_a = store.issue(
        actor="owner",
        tool_name="run_shell",
        args={"command": "echo a"},
        issuer="policy",
    )
    token_b = store.issue(
        actor="owner",
        tool_name="run_shell",
        args={"command": "echo b"},
        issuer="policy",
    )

    assert token_a and token_b
    assert token_a != token_b

    with sqlite3.connect(db_path) as conn:
        request_ids = [
            row[0]
            for row in conn.execute(
                "SELECT request_id FROM approval_grants ORDER BY issued_at"
            )
        ]
    assert len(request_ids) == 2
    assert all(rid for rid in request_ids)
    assert len(set(request_ids)) == 2


def _engine(tmp_path) -> PolicyEngine:
    store = ApprovalGrantStore(db_path=str(tmp_path / "grants.sqlite"))
    return PolicyEngine(require_approval=True, grant_store=store)


@pytest.mark.asyncio
async def test_full_cycle_request_policy_approval_redispath(tmp_path):
    eng = _engine(tmp_path)

    # REQUEST -> POLICY (CRITICAL requires 2-step)
    verdict = await eng.check("run_shell", params={"command": "rm -rf /tmp/x"}, context=CTX)
    assert verdict["allowed"] is False
    assert verdict["requires_2step_confirmation"] is True
    pending = verdict["pending_confirmation"]
    assert pending.tool_name == "run_shell"
    # No durable grant exists yet at REQUEST time.
    assert pending.grant_token == ""

    # APPROVAL (exact phrase) mints the durable grant.
    ok, _msg, approved = eng.verify_confirmation("cli", "owner", "s1", pending.exact_phrase)
    assert ok is True
    assert approved is not None and approved.grant_token != ""

    # RE-DISPATCH consumes the grant exactly once (valid first time).
    first = eng.grant_store.verify_and_consume(
        approved.grant_token,
        actor="owner", tool_name="run_shell", args={"command": "rm -rf /tmp/x"},
    )
    assert first.valid is True

    # Replay the same grant token => consumed => DENIED (one-shot).
    replay = eng.grant_store.verify_and_consume(
        approved.grant_token,
        actor="owner", tool_name="run_shell", args={"command": "rm -rf /tmp/x"},
    )
    assert replay.valid is False
    assert str(replay.reason) == "grant_consumed"


@pytest.mark.asyncio
async def test_confirm_and_continue_bridge_allows_once(tmp_path):
    eng = _engine(tmp_path)
    verdict = await eng.check("run_shell", params={"command": "rm -rf /tmp/x"}, context=CTX)
    pending = verdict["pending_confirmation"]

    # APPROVAL + RE-DISPATCH in one call => allowed once.
    first = await eng.confirm_and_continue(
        "cli", "owner", "s1", pending.exact_phrase,
        tool_name="run_shell", args={"command": "rm -rf /tmp/x"},
    )
    assert first["allowed"] is True

    # Second confirm with the same phrase => pending already consumed => denied.
    second = await eng.confirm_and_continue(
        "cli", "owner", "s1", pending.exact_phrase,
        tool_name="run_shell", args={"command": "rm -rf /tmp/x"},
    )
    assert second["allowed"] is False


@pytest.mark.asyncio
async def test_redispath_wrong_tool_denied(tmp_path):
    eng = _engine(tmp_path)
    verdict = await eng.check("run_shell", params={"command": "rm -rf /tmp/x"}, context=CTX)
    pending = verdict["pending_confirmation"]

    # Grant is minted bound to run_shell/args; re-dispatch asks for write_file
    # => TOOL_MISMATCH => denied.
    wrong = await eng.confirm_and_continue(
        "cli", "owner", "s1", pending.exact_phrase,
        tool_name="write_file", args={"path": "x.txt"},
    )
    assert wrong["allowed"] is False
    assert "grant_tool_mismatch" in wrong["reason"]


@pytest.mark.asyncio
async def test_redispath_wrong_args_denied(tmp_path):
    eng = _engine(tmp_path)
    verdict = await eng.check("run_shell", params={"command": "rm -rf /tmp/x"}, context=CTX)
    pending = verdict["pending_confirmation"]

    wrong = await eng.confirm_and_continue(
        "cli", "owner", "s1", pending.exact_phrase,
        tool_name="run_shell", args={"command": "echo hacked"},  # different args
    )
    assert wrong["allowed"] is False
    assert "grant_args_mismatch" in wrong["reason"]


@pytest.mark.asyncio
async def test_actor_binding_enforced(tmp_path):
    """A grant issued to 'owner' cannot be consumed by another actor."""
    eng = _engine(tmp_path)
    verdict = await eng.check("run_shell", params={"command": "rm -rf /tmp/x"}, context=CTX)
    pending = verdict["pending_confirmation"]
    ok, _m, approved = eng.verify_confirmation("cli", "owner", "s1", pending.exact_phrase)
    assert ok is True and approved.grant_token

    stolen = eng.grant_store.verify_and_consume(
        approved.grant_token,
        actor="intruder", tool_name="run_shell", args={"command": "rm -rf /tmp/x"},
    )
    assert stolen.valid is False
    assert str(stolen.reason) == "grant_actor_mismatch"


@pytest.mark.asyncio
async def test_simple_words_still_rejected(tmp_path):
    eng = _engine(tmp_path)
    await eng.check("run_shell", params={"command": "rm -rf /tmp/x"}, context=CTX)
    ok, msg, _ = eng.verify_confirmation("cli", "owner", "s1", "да")
    assert ok is False
    assert "просты" in msg.lower() or "не принимают" in msg
