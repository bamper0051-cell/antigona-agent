"""A-1/A-3/A-4 — the canonical ApprovalGrant is wired into the live paths.

One mechanism authorizes every owner confirmation: ``ApprovalGrantStore``.
These tests prove the wiring, not the store itself (see
``test_approval_grant_confirm.py`` for the store contract):

* A-4  ``/confirm`` resolves a real pending confirmation and mints a grant,
       and the canonical bridge consumes it exactly once.
* A-3  ``UnifiedToolExecutionLayer`` verifies ``approval_token`` against the
       store — a forged (non-empty) token is refused, a real grant works once.
* A-1  an owner approval decision mints a durable one-shot grant which the
       worker consumes exactly once (no more "one approval, unlimited shells").
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from antigona.channels.telegram import auth_handlers
from antigona.database import Database
from antigona.engine.unified_executor import (
    OwnerAuthContext,
    ToolExecutionRequest,
    UnifiedToolExecutionLayer,
)
from antigona.policy.engine import PolicyEngine, confirm_pending_globally
from antigona.repository import CreateTask, TaskRepository
from antigona.security.approval_grant import ApprovalGrantStore
from antigona.tools import pin_gate
from antigona.worker.agent_core import WorkerAgentCore

CRITICAL_COMMAND = "rm -rf /tmp/a1-target"


class _DummyMessage:
    def __init__(self, chat_id: int, user_id: int, text: str) -> None:
        self.chat = SimpleNamespace(id=chat_id)
        self.from_user = SimpleNamespace(id=user_id)
        self.text = text
        self.answers: list[str] = []

    async def answer(self, text: str) -> None:
        self.answers.append(text)

    async def delete(self) -> None:  # pragma: no cover - never used here
        return None


@pytest.fixture
def store(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> ApprovalGrantStore:
    """An isolated grant store, also used by code that builds its own."""
    db_path = tmp_path / "grants.sqlite"
    monkeypatch.setattr(
        "antigona.core.paths.database_path", lambda *a, **k: str(db_path)
    )
    return ApprovalGrantStore(db_path=str(db_path))


# ── A-4: /confirm resolves a pending confirmation and mints a grant ─────────


@pytest.mark.asyncio
async def test_confirm_pending_globally_consumes_grant_exactly_once(
    store: ApprovalGrantStore,
) -> None:
    engine = PolicyEngine(require_approval=True, grant_store=store)
    verdict = await engine.check(
        "run_shell",
        params={"command": CRITICAL_COMMAND},
        context={"channel": "telegram", "user_id": "42", "session_id": "s1"},
    )
    pending = verdict["pending_confirmation"]

    first = await confirm_pending_globally("telegram", 42, f"/confirm {pending.token}")
    assert first is not None and first["allowed"] is True

    # Replay of the very same token: the pending is gone, the grant is spent.
    second = await confirm_pending_globally("telegram", 42, f"/confirm {pending.token}")
    assert second is None or second["allowed"] is False


@pytest.mark.asyncio
async def test_confirm_pending_globally_rejects_foreign_user(
    store: ApprovalGrantStore,
) -> None:
    engine = PolicyEngine(require_approval=True, grant_store=store)
    verdict = await engine.check(
        "run_shell",
        params={"command": CRITICAL_COMMAND},
        context={"channel": "telegram", "user_id": "42", "session_id": "s1"},
    )
    pending = verdict["pending_confirmation"]

    stolen = await confirm_pending_globally("telegram", 777, f"/confirm {pending.token}")
    assert stolen is not None and stolen["allowed"] is False
    # The rightful owner can still confirm — a foreign attempt consumes nothing.
    ok = await confirm_pending_globally("telegram", 42, f"/confirm {pending.token}")
    assert ok is not None and ok["allowed"] is True


@pytest.mark.anyio
async def test_cmd_confirm_resolves_policy_pending_and_mints_grant(
    store: ApprovalGrantStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTIGONA_OWNER_ID", "42")
    engine = PolicyEngine(require_approval=True, grant_store=store)
    verdict = await engine.check(
        "run_shell",
        params={"command": CRITICAL_COMMAND},
        context={"channel": "telegram", "user_id": "42", "session_id": "s1"},
    )
    pending = verdict["pending_confirmation"]

    message = _DummyMessage(955_222, 42, f"/confirm {pending.token}")
    result = await auth_handlers.cmd_confirm(message)

    assert result == "CONFIRMED"
    assert "подтверждено" in message.answers[0]

    # Second /confirm with the same token: nothing left to confirm.
    replay = _DummyMessage(955_222, 42, f"/confirm {pending.token}")
    assert await auth_handlers.cmd_confirm(replay) is None
    assert "Неверный или истёкший" in replay.answers[0]


@pytest.mark.anyio
async def test_pin_gate_confirmation_mints_one_shot_grant(
    store: ApprovalGrantStore,
) -> None:
    pin_gate.reset_all_sessions()
    chat_id, user_id = 955_333, 42
    pin_gate.elevate_session(chat_id, user_id)
    payload = {"path": "/tmp/a1.txt"}
    cid = pin_gate.set_pending_confirmation(chat_id, "DELETE_FILE", payload)

    granted = pin_gate.confirm_action_with_grant(
        chat_id, user_id, cid, grant_store=store
    )
    assert granted is not None
    confirmed_payload, token = granted
    assert confirmed_payload == payload
    assert token

    first = store.verify_and_consume(
        token, actor=str(user_id), tool_name="DELETE_FILE", args=payload
    )
    assert first.valid is True
    replay = store.verify_and_consume(
        token, actor=str(user_id), tool_name="DELETE_FILE", args=payload
    )
    assert replay.valid is False
    pin_gate.reset_all_sessions()


# ── A-3: unified_executor verifies approval_token against the store ─────────


def _shell_request(command: str, token: str) -> ToolExecutionRequest:
    return ToolExecutionRequest(
        tool_name="run_shell",
        params={"command": command},
        requester="llm",
        user_id="owner",
        correlation_id=str(uuid.uuid4()),
        turn_id=str(uuid.uuid4()),
        owner_auth=OwnerAuthContext(
            approval_token=token, approved_command_text=command
        ),
    )


@pytest.mark.asyncio
async def test_unified_executor_rejects_forged_approval_token(
    store: ApprovalGrantStore,
) -> None:
    unified = UnifiedToolExecutionLayer(grant_store=store)
    result = json.loads(
        await unified.execute(_shell_request("echo forged", "not-a-real-grant"))
    )
    assert "error" in result
    assert result.get("success") is not True


@pytest.mark.asyncio
async def test_unified_executor_accepts_real_grant_once(
    store: ApprovalGrantStore,
) -> None:
    unified = UnifiedToolExecutionLayer(grant_store=store)
    command = "echo unified-grant-ok"
    token = store.issue(
        actor="owner",
        tool_name="run_shell",
        args={"command": command},
        issuer="test",
    )

    first = json.loads(await unified.execute(_shell_request(command, token)))
    assert "error" not in first

    # The grant is one-shot: the same token cannot authorize a second run.
    second = json.loads(await unified.execute(_shell_request(command, token)))
    assert "error" in second


@pytest.mark.asyncio
async def test_unified_executor_grant_bound_to_exact_command(
    store: ApprovalGrantStore,
) -> None:
    unified = UnifiedToolExecutionLayer(grant_store=store)
    token = store.issue(
        actor="owner",
        tool_name="run_shell",
        args={"command": "echo approved"},
        issuer="test",
    )
    result = json.loads(await unified.execute(_shell_request("echo something-else", token)))
    assert "error" in result


# ── A-1: owner approval mints a durable one-shot grant ─────────────────────


def _approved_task(database: Database) -> tuple[Any, Any]:
    with database.session_factory() as session:
        repository = TaskRepository(session)
        task, _ = repository.create(
            CreateTask(
                owner_id="owner",
                goal="print a fixed greeting",
                path="reports/shell.txt",
                content="hello",
                idempotency_key=f"a1-{uuid.uuid4()}",
                tool_name="sandbox.shell",
                command=("printf", "hello"),
            )
        )
        approval = repository.request_approval(task)
        approval.decision = "PENDING"
        session.commit()
        decided = repository.decide_approval(task, approval.id, "owner", True)
        return task, decided


def test_owner_approval_mints_grant_and_worker_consumes_it_once(
    store: ApprovalGrantStore,
) -> None:
    database = Database("sqlite:///:memory:")
    database.create_all()
    task, approval = _approved_task(database)

    assert approval.decision == "APPROVED"
    assert approval.grant_token, "owner approval must mint a durable grant"

    core = object.__new__(WorkerAgentCore)
    core._current_task = SimpleNamespace(
        id=task.id, tool_name="sandbox.shell", approvals=[approval]
    )
    # First shell call consumes the grant, the second is no longer approved.
    assert core._has_approved_shell_approval() is True
    assert core._has_approved_shell_approval() is False


def test_auto_approved_flow_needs_no_grant(store: ApprovalGrantStore) -> None:
    """B5/L7-1 write→run: deterministic auto-approval is not an owner grant."""
    core = object.__new__(WorkerAgentCore)
    core._current_task = SimpleNamespace(
        id="t1",
        tool_name="sandbox.shell",
        approvals=[
            SimpleNamespace(
                tool_name="sandbox.shell",
                decision="APPROVED",
                grant_token=None,
                arguments={},
                decided_by=None,
            )
        ],
    )
    assert core._has_approved_shell_approval() is True
    assert core._has_approved_shell_approval() is True


def test_denied_decision_mints_no_grant(store: ApprovalGrantStore) -> None:
    database = Database("sqlite:///:memory:")
    database.create_all()
    with database.session_factory() as session:
        repository = TaskRepository(session)
        task, _ = repository.create(
            CreateTask(
                owner_id="owner",
                goal="print a fixed greeting",
                path="reports/shell.txt",
                content="hello",
                idempotency_key=f"a1-denied-{uuid.uuid4()}",
                tool_name="sandbox.shell",
                command=("printf", "hello"),
            )
        )
        approval = repository.request_approval(task)
        approval.decision = "PENDING"
        session.commit()
        decided = repository.decide_approval(task, approval.id, "owner", False)

    assert decided.decision == "DENIED"
    assert not decided.grant_token
