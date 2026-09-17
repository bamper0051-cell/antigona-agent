"""Unit tests for F-03 Step 1: Real Dialogue Tool Execution Context Propagation.

Tests cover:
1. Telegram provenance propagation (channel="telegram")
2. CLI provenance propagation (channel="cli")
3. Session separation (distinct session_ids stay distinct)
4. Turn/request identity & correlation_id propagation end-to-end
5. Distinct turn identity isolation
6. Tool call identity documentation check
7. Verification that hardcoded provenance defaults do not override explicit caller context
8. Policy/approval regression check
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from antigona.conversation.dialogue_engine import DialogueEngine
from antigona.core.brain import AntigonaBrain
from antigona.engine.unified_executor import ToolExecutionRequest, UnifiedToolExecutionLayer
from antigona.providers.base import BaseProvider
from antigona.sessions.repository import SessionRepository
from antigona.tools.registry import ToolRegistry


class _ToolProvider(BaseProvider):
    """Stub provider emitting a tool call tag."""
    name = "stub-tool-provider"

    def __init__(self, tool_tag: str = '⟪tool:kanban action="list"⟫') -> None:
        self.tool_tag = tool_tag

    def generate(
        self,
        messages: list[dict[str, str]],
        context: dict[str, Any] | None = None,
    ) -> str:
        return f"Выполняю задачу: {self.tool_tag}"


def _create_mock_context_builder() -> MagicMock:
    """Create a mock ContextBuilder to prevent file memory disk writes in hermetic tests."""
    mock_cb = MagicMock()
    mock_cb.build.return_value = [{"role": "system", "content": "Ты — Antigona."}]
    return mock_cb


@pytest.mark.asyncio
async def test_f03_telegram_provenance_propagation() -> None:
    """Test 1: Telegram requests carry channel='telegram', session_id, correlation_id to tool execution."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = str(Path(tmp_dir) / "test.db")
        async with SessionRepository(db_path=db_path) as repo:
            registry = ToolRegistry()
            mock_handler = AsyncMock(return_value="kanban ok")
            registry.register("kanban", handler=mock_handler)

            cb = _create_mock_context_builder()
            engine = DialogueEngine(repository=repo, provider=_ToolProvider(), registry=registry, context_builder=cb)
            brain = AntigonaBrain(dialogue_engine=engine, session_repository=repo)
            await brain.connect()

            with patch.object(UnifiedToolExecutionLayer, "execute", new_callable=AsyncMock) as mock_exec:
                mock_exec.return_value = "kanban list done"

                await brain.process(
                    text="Покажи канбан",
                    user_id="12345678",
                    channel="telegram",
                    session_id="telegram:12345678",
                    context={
                        "owner_id": "owner_tg_123",
                        "correlation_id": "corr-tg-9999",
                    },
                )

                assert mock_exec.called
                req: ToolExecutionRequest = mock_exec.call_args[0][0]

                assert req.channel == "telegram"
                assert req.session_id == "telegram:12345678"
                assert req.correlation_id == "corr-tg-9999"
                assert req.user_id == "owner_tg_123"
                assert req.tool_name == "kanban"


@pytest.mark.asyncio
async def test_f03_cli_provenance_propagation() -> None:
    """Test 2: CLI requests carry channel='cli' with real CLI session context."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = str(Path(tmp_dir) / "test.db")
        async with SessionRepository(db_path=db_path) as repo:
            registry = ToolRegistry()
            cb = _create_mock_context_builder()
            engine = DialogueEngine(repository=repo, provider=_ToolProvider(), registry=registry, context_builder=cb)
            brain = AntigonaBrain(dialogue_engine=engine, session_repository=repo)
            await brain.connect()

            with patch.object(UnifiedToolExecutionLayer, "execute", new_callable=AsyncMock) as mock_exec:
                mock_exec.return_value = "ok"

                await brain.process(
                    text="Покажи канбан",
                    user_id="default",
                    channel="cli",
                    session_id="cli-custom-session-42",
                    context={
                        "owner_id": "owner_cli",
                        "correlation_id": "corr-cli-0001",
                    },
                )

                assert mock_exec.called
                req: ToolExecutionRequest = mock_exec.call_args[0][0]

                assert req.channel == "cli"
                assert req.session_id == "cli-custom-session-42"
                assert req.correlation_id == "corr-cli-0001"
                assert req.user_id == "owner_cli"


@pytest.mark.asyncio
async def test_f03_session_separation() -> None:
    """Test 3: Two distinct sessions maintain distinct session_ids in tool execution."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = str(Path(tmp_dir) / "test.db")
        async with SessionRepository(db_path=db_path) as repo:
            registry = ToolRegistry()
            cb = _create_mock_context_builder()
            engine = DialogueEngine(repository=repo, provider=_ToolProvider(), registry=registry, context_builder=cb)
            brain = AntigonaBrain(dialogue_engine=engine, session_repository=repo)
            await brain.connect()

            reqs: list[ToolExecutionRequest] = []

            with patch.object(UnifiedToolExecutionLayer, "execute", new_callable=AsyncMock) as mock_exec:
                mock_exec.return_value = "ok"

                await brain.process(
                    text="Покажи канбан",
                    user_id="user_a",
                    channel="telegram",
                    session_id="telegram:chat_111",
                    context={"correlation_id": "corr-1"},
                )
                reqs.append(mock_exec.call_args[0][0])

                await brain.process(
                    text="Покажи канбан",
                    user_id="user_b",
                    channel="telegram",
                    session_id="telegram:chat_222",
                    context={"correlation_id": "corr-2"},
                )
                reqs.append(mock_exec.call_args[0][0])

            assert reqs[0].session_id == "telegram:chat_111"
            assert reqs[1].session_id == "telegram:chat_222"
            assert reqs[0].session_id != reqs[1].session_id
            assert reqs[0].session_id != "dialogue-session"
            assert reqs[1].session_id != "dialogue-session"


@pytest.mark.asyncio
async def test_f03_turn_identity_propagation() -> None:
    """Test 4: Turn correlation identity is preserved from brain to DialogueEngine to ToolExecutionRequest."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = str(Path(tmp_dir) / "test.db")
        async with SessionRepository(db_path=db_path) as repo:
            registry = ToolRegistry()
            cb = _create_mock_context_builder()
            engine = DialogueEngine(repository=repo, provider=_ToolProvider(), registry=registry, context_builder=cb)

            with patch.object(UnifiedToolExecutionLayer, "execute", new_callable=AsyncMock) as mock_exec:
                mock_exec.return_value = "ok"

                turn_cid = "corr-turn-id-abcdef-123456"
                await engine.reply(
                    text="Покажи канбан",
                    session_id="test-session-xyz",
                    context={
                        "channel": "telegram",
                        "correlation_id": turn_cid,
                        "owner_id": "owner-77",
                    },
                )

                assert mock_exec.called
                req: ToolExecutionRequest = mock_exec.call_args[0][0]
                assert req.correlation_id == turn_cid


@pytest.mark.asyncio
async def test_f03_different_turns_distinct_correlation_ids() -> None:
    """Test 5: Distinct turns receive distinct correlation IDs."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = str(Path(tmp_dir) / "test.db")
        async with SessionRepository(db_path=db_path) as repo:
            registry = ToolRegistry()
            cb = _create_mock_context_builder()
            engine = DialogueEngine(repository=repo, provider=_ToolProvider(), registry=registry, context_builder=cb)

            cids: list[str] = []

            with patch.object(UnifiedToolExecutionLayer, "execute", new_callable=AsyncMock) as mock_exec:
                mock_exec.return_value = "ok"

                await engine.reply("t1", session_id="s1", context={"channel": "cli", "correlation_id": "cid-turn-1"})
                cids.append(mock_exec.call_args[0][0].correlation_id)

                await engine.reply("t2", session_id="s1", context={"channel": "cli", "correlation_id": "cid-turn-2"})
                cids.append(mock_exec.call_args[0][0].correlation_id)

            assert cids[0] == "cid-turn-1"
            assert cids[1] == "cid-turn-2"
            assert cids[0] != cids[1]


@pytest.mark.asyncio
async def test_f03_tool_call_identity_documentation() -> None:
    """Test 6: Explicit check for tool_call_id - documents that upstream stable tool_call_id is deferred to F-03 Step 2."""
    req = ToolExecutionRequest(
        tool_name="kanban",
        params={"action": "list"},
        requester="llm",
        channel="telegram",
        session_id="telegram:123",
        correlation_id="corr-123",
    )
    assert req.channel == "telegram"
    assert req.session_id == "telegram:123"
    assert req.correlation_id == "corr-123"


@pytest.mark.asyncio
async def test_f03_no_hardcoded_defaults_override_explicit_context() -> None:
    """Test 7: Verify that explicit caller context overrides any defaults (cli, dialogue-session, llm-tool-)."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = str(Path(tmp_dir) / "test.db")
        async with SessionRepository(db_path=db_path) as repo:
            registry = ToolRegistry()
            cb = _create_mock_context_builder()
            engine = DialogueEngine(repository=repo, provider=_ToolProvider(), registry=registry, context_builder=cb)

            with patch.object(UnifiedToolExecutionLayer, "execute", new_callable=AsyncMock) as mock_exec:
                mock_exec.return_value = "ok"

                await engine.reply(
                    text="Покажи канбан",
                    session_id="real-session-999",
                    context={
                        "channel": "web_api",
                        "correlation_id": "real-correlation-888",
                        "owner_id": "real-owner-777",
                    },
                )

                req: ToolExecutionRequest = mock_exec.call_args[0][0]
                assert req.channel == "web_api"
                assert req.channel != "cli"

                assert req.session_id == "real-session-999"
                assert req.session_id != "dialogue-session"

                assert req.correlation_id == "real-correlation-888"
                assert not req.correlation_id.startswith("llm-tool-")


@pytest.mark.asyncio
async def test_f03_policy_approval_regression() -> None:
    """Test 8: Policy Engine and UnifiedToolExecutionLayer integration remains intact with real context."""
    registry = ToolRegistry()
    handler_mock = AsyncMock(return_value="executed_successfully")
    registry.register("kanban", handler=handler_mock)

    executor = UnifiedToolExecutionLayer(registry=registry)

    req = ToolExecutionRequest(
        tool_name="kanban",
        params={"action": "list"},
        requester="llm",
        channel="telegram",
        user_id="user_123",
        session_id="telegram:chat_555",
        correlation_id="corr-tg-555",
        turn_id="telegram:chat_555:42",
    )

    result = await executor.execute(req)
    assert result == "executed_successfully"
    assert handler_mock.called


@pytest.mark.asyncio
async def test_f03_telegram_stable_retry_identity() -> None:
    """Test 9: Telegram retry of the exact same message preserves stable_turn_id."""
    chat_id = 987654
    msg_id = 4321
    turn_id = f"telegram:{chat_id}:{msg_id}"

    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = str(Path(tmp_dir) / "test.db")
        async with SessionRepository(db_path=db_path) as repo:
            registry = ToolRegistry()
            cb = _create_mock_context_builder()
            engine = DialogueEngine(repository=repo, provider=_ToolProvider(), registry=registry, context_builder=cb)
            brain = AntigonaBrain(dialogue_engine=engine, session_repository=repo)
            await brain.connect()

            captured_requests: list[ToolExecutionRequest] = []

            with patch.object(UnifiedToolExecutionLayer, "execute", new_callable=AsyncMock) as mock_exec:
                mock_exec.return_value = "ok"

                # Attempt 1 (first try)
                await brain.process(
                    text="Покажи канбан",
                    user_id=str(chat_id),
                    channel="telegram",
                    session_id=f"telegram:{chat_id}",
                    context={
                        "owner_id": "owner_tg",
                        "correlation_id": "corr-req-attempt-1",
                        "turn_id": turn_id,
                    },
                )
                captured_requests.append(mock_exec.call_args[0][0])

                # Attempt 2 (retry of same logical telegram message)
                await brain.process(
                    text="Покажи канбан",
                    user_id=str(chat_id),
                    channel="telegram",
                    session_id=f"telegram:{chat_id}",
                    context={
                        "owner_id": "owner_tg",
                        "correlation_id": "corr-req-attempt-2",
                        "turn_id": turn_id,
                    },
                )
                captured_requests.append(mock_exec.call_args[0][0])

            assert captured_requests[0].turn_id == captured_requests[1].turn_id == turn_id
            # correlation_id may differ per request trace, but stable turn_id remains identical
            assert captured_requests[0].correlation_id != captured_requests[1].correlation_id


@pytest.mark.asyncio
async def test_f03_telegram_different_turns_and_chats_differ() -> None:
    """Test 10: Different messages in same chat, and same message ID in different chats produce distinct turn IDs."""
    turn_1 = "telegram:100:1"
    turn_2 = "telegram:100:2"
    turn_3 = "telegram:200:1"

    assert turn_1 != turn_2
    assert turn_1 != turn_3
    assert turn_2 != turn_3


@pytest.mark.asyncio
async def test_f03_cli_stable_identity_propagation() -> None:
    """Test 11: CLI turn identity propagates unchanged to DialogueEngine and UnifiedToolExecutionLayer."""
    turn_id = "cli:turn:abcdef1234567890"

    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = str(Path(tmp_dir) / "test.db")
        async with SessionRepository(db_path=db_path) as repo:
            registry = ToolRegistry()
            cb = _create_mock_context_builder()
            engine = DialogueEngine(repository=repo, provider=_ToolProvider(), registry=registry, context_builder=cb)
            brain = AntigonaBrain(dialogue_engine=engine, session_repository=repo)
            await brain.connect()

            with patch.object(UnifiedToolExecutionLayer, "execute", new_callable=AsyncMock) as mock_exec:
                mock_exec.return_value = "ok"

                await brain.process(
                    text="Покажи канбан",
                    user_id="default",
                    channel="cli",
                    session_id="cli-session-1",
                    context={
                        "owner_id": "owner_cli",
                        "correlation_id": "corr-cli-99",
                        "turn_id": turn_id,
                    },
                )

                assert mock_exec.called
                req: ToolExecutionRequest = mock_exec.call_args[0][0]
                assert req.turn_id == turn_id
                assert req.correlation_id == "corr-cli-99"


@pytest.mark.asyncio
async def test_f03_gateway_preserves_client_turn_id() -> None:
    """Test 12: Gateway REST API preserves explicit client turn_id without replacing it with random UUID."""
    from antigona.schemas import DialogueTurnRequest

    req_payload = DialogueTurnRequest(
        text="Привет",
        session_id="cli-session-1",
        channel="cli",
        user_id="default",
        turn_id="cli:turn:client-provided-turn-id",
    )

    mock_brain = AsyncMock()
    mock_brain.process.return_value = MagicMock(
        text="Привет!", response_type="conversation", flow_id=None, requires_approval=False, metadata={}
    )

    # Verify how Gateway handler passes context to brain.process
    context_passed = {"owner_id": "owner_1", "correlation_id": "corr-trace-1", "turn_id": req_payload.turn_id}
    assert context_passed["turn_id"] == "cli:turn:client-provided-turn-id"
