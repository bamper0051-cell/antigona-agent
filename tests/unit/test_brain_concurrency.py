"""Concurrency-тест единого ядра (Stage 1): параллельные turn от разных owners.

Проверяет, что AntigonaBrain (общий серверный singleton) НЕ смешивает
per-request контекст: owner_id / session_id / correlation_id передаются явными
аргументами и не хранятся на общем объекте (защита от cross-request races).
"""

from __future__ import annotations

import asyncio
import tempfile
from typing import Any

import pytest

from antigona.core.brain import AntigonaBrain, ResponseType


class _RecordingBackend:
    """TaskBackend, записывающий owner_id/correlation_id каждого submit."""

    def __init__(self) -> None:
        self.submits: list[tuple[str, str]] = []  # (owner_id, correlation_id)

    async def submit_task(
        self,
        message: str,
        conversation_id: str = "",
        client: str = "cli",
        metadata: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        owner_id: str | None = None,
        correlation_id: str | None = None,
        tool_name: str | None = None,
        command: tuple[str, ...] = (),
        path: str | None = None,
        content: str | None = None,
        mcp_server: str = "",
        mcp_tool: str = "",
        mcp_arguments: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        owner = owner_id or ""
        corr = correlation_id or ""
        self.submits.append((owner, corr))
        # Разные сообщения → разные flow_id, чтобы видеть, кто что создал.
        return {
            "flow_id": f"flow-{owner}-{corr[-4:]}",
            "id": f"flow-{owner}-{corr[-4:]}",
            "status": "QUEUED",
            "requires_approval": False,
        }

    async def cancel_flow(self, flow_id: str) -> Any:
        return None

    async def get_flow(self, flow_id: str) -> Any:
        return type("FV", (), {"status": "QUEUED"})()


@pytest.mark.asyncio
async def test_parallel_turns_keep_owner_and_correlation_isolated() -> None:
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as tmp_db:
        backend = _RecordingBackend()
        brain = AntigonaBrain(db_path=tmp_db.name, task_backend=backend)
        await brain.connect()
        try:
            async def turn(owner: str, corr: str, msg: str) -> tuple[str, str, str, str]:
                resp = await brain.process(
                    text=msg,
                    user_id=owner,
                    channel="cli",
                    session_id=f"{owner}:session",
                    context={"owner_id": owner, "correlation_id": corr},
                )
                return (owner, corr, resp.response_type, resp.flow_id or "")

            results = await asyncio.gather(
                turn("owner-a", "corr-aaaa", "создай файл a.txt"),
                turn("owner-b", "corr-bbbb", "создай файл b.txt"),
                turn("owner-c", "corr-cccc", "создай файл c.txt"),
            )

            # Каждый flow принадлежит правильному owner.
            by_owner = {r[0]: r for r in results}
            assert by_owner["owner-a"][2] == ResponseType.TASK_ACCEPTED
            assert by_owner["owner-a"][3] == "flow-owner-a-aaaa"
            assert by_owner["owner-b"][3] == "flow-owner-b-bbbb"
            assert by_owner["owner-c"][3] == "flow-owner-c-cccc"

            # Backend видел ровно 3 submit'а, каждый со своим owner/correlation.
            assert len(backend.submits) == 3
            pairs = {(o, c) for (o, c) in backend.submits}
            assert pairs == {
                ("owner-a", "corr-aaaa"),
                ("owner-b", "corr-bbbb"),
                ("owner-c", "corr-cccc"),
            }
        finally:
            await brain.close()


@pytest.mark.asyncio
async def test_owner_context_not_stored_on_shared_singleton() -> None:
    """После process() на объекте не остаётся _current_context с чужим owner."""
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as tmp_db:
        backend = _RecordingBackend()
        brain = AntigonaBrain(db_path=tmp_db.name, task_backend=backend)
        await brain.connect()
        try:
            await brain.process(
                text="создай файл x.txt",
                user_id="alice",
                channel="cli",
                session_id="alice:session",
                context={"owner_id": "alice", "correlation_id": "corr-0001"},
            )
            # Нет mutable per-request state на singleton.
            assert not hasattr(brain, "_current_context")
            assert not hasattr(brain, "_current_owner")
        finally:
            await brain.close()


@pytest.mark.asyncio
async def test_foreign_session_id_preserved_but_tasks_owner_scoped() -> None:
    """P-01 owner/session isolation per the canonical contract.

    A client-supplied session_id identifies a CONVERSATION, not an identity: it
    must propagate unchanged (F-03 I-PROPAGATE) and is NEVER rewritten to
    ``{channel}:{owner_id}``. Owner isolation is guaranteed by the authenticated
    owner_id (gateway Bearer token) being threaded into task creation — tasks
    are owner-scoped regardless of the conversation id the client chose.
    """
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as tmp_db:
        backend = _RecordingBackend()
        brain = AntigonaBrain(db_path=tmp_db.name, task_backend=backend)
        await brain.connect()
        try:
            resp = await brain.process(
                text="создай файл x.txt",
                user_id="alice",
                channel="cli",
                session_id="eve:session",  # foreign conversation id — not rewritten
                context={"owner_id": "alice", "correlation_id": "corr-0001"},
            )
            # Task accepted and attributed to the authenticated owner (alice).
            assert resp.response_type == ResponseType.TASK_ACCEPTED
            assert resp.flow_id == "flow-alice-0001"
            # Backend saw alice as owner — never eve.
            assert backend.submits and backend.submits[0][0] == "alice"
            # The client session id exists unchanged — it was NOT rescoped.
            assert await brain._session_repo.session_exists("eve:session") is True
            assert await brain._session_repo.session_exists("cli:alice") is False
        finally:
            await brain.close()
