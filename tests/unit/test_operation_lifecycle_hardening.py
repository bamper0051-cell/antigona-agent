"""Red-regression package for the durable Telegram operation lifecycle.

All probes use a temporary SQLite database and mocked Telegram/Gateway calls.
They intentionally exercise real OperationStore CAS/claim behavior rather than
replacing persistence with mocks.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from antigona.channels.telegram.bot import TelegramBot
from antigona.core.control_plane import FlowStatus
from antigona.durable.operation_models import OperationState
from antigona.events.event_types import (
    FinalResponseReady,
    OperationReceived,
    StageChanged,
)
from antigona.input_pipeline.models import ProcessingOutcome, ProcessingResult
from antigona.presentation.presenter import OperationPresenter
from antigona.security.owner_identity import OwnerIdentity


def _message(
    text: str = "задача",
    *,
    chat_id: int = 700,
    message_id: int = 1,
) -> MagicMock:
    message = MagicMock()
    message.text = text
    message.chat = MagicMock()
    message.chat.id = chat_id
    message.chat.type = "private"
    message.message_id = message_id
    message.from_user = MagicMock()
    message.from_user.id = 701
    message.from_user.is_bot = False
    message.reply_to_message = None
    message.bot = MagicMock()
    message.bot.send_chat_action = AsyncMock()
    message.answer = AsyncMock(return_value=MagicMock(message_id=900))
    return message


def _reply_message(
    text: str,
    *,
    chat_id: int,
    message_id: int,
    reply_to_message_id: int,
) -> MagicMock:
    message = _message(text, chat_id=chat_id, message_id=message_id)
    quoted = MagicMock()
    quoted.message_id = reply_to_message_id
    quoted.text = "progress"
    quoted.caption = None
    quoted.from_user = MagicMock()
    quoted.from_user.id = 999
    quoted.from_user.is_bot = True
    quoted.from_user.full_name = "Antigona"
    quoted.from_user.username = "antigona_bot"
    message.reply_to_message = quoted
    return message


def _text_handler(bot: TelegramBot):
    return next(
        handler.callback
        for handler in bot.router.message.handlers
        if handler.callback.__name__ == "text_handler"
    )


def _result(
    *,
    outcome: ProcessingOutcome,
    terminal: bool,
    flow_status: FlowStatus | None = None,
    response_text: str | None = None,
    success: bool = True,
    error: str | None = None,
    task_id: str = "flow-1",
) -> ProcessingResult:
    return ProcessingResult(
        success=success,
        task_id=task_id,
        session_id=task_id,
        response_text=response_text,
        error=error,
        correlation_id="corr-1",
        duration_ms=1.0,
        outcome=outcome,
        terminal=terminal,
        flow_status=flow_status,
    )


@pytest.fixture
async def bot_instance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[TelegramBot]:
    monkeypatch.setenv("ANTIGONA_ENABLE_STARTUP_RECOVERY", "0")
    # Keep the durable turn ledger inside the test's tmp dir.
    monkeypatch.setenv(
        "ANTIGONA_TELEGRAM_TURN_LEDGER",
        str(tmp_path / "turns.db"),
    )
    # ``_resolve_bot_info`` would otherwise call the real Telegram API, which
    # both breaks hermeticity and opens the aiohttp session these probes then
    # leak.  Pin the identity instead.
    monkeypatch.setattr(
        TelegramBot,
        "_resolve_bot_info",
        AsyncMock(return_value=("antigona_test_bot", 424242)),
    )
    # These lifecycle probes exercise the durable operation machinery, not the
    # owner gate.  Accept every test user so the probes are independent of any
    # ANTIGONA_OWNER_ID configured in the ambient environment.
    monkeypatch.setattr(OwnerIdentity, "is_owner", lambda self, user_id: True)
    bot = TelegramBot(
        token="123456789:ABCdefGHIjklMNOpqrsTUVwxyz",
        gateway_url="http://test-gateway:8090",
        gateway_token="test-token",
        database_url=f"sqlite:///{tmp_path / 'hardening.db'}",
    )
    next_receipt = 1000

    async def _send_message(**kwargs):
        nonlocal next_receipt
        next_receipt += 1
        return SimpleNamespace(message_id=next_receipt)

    bot.bot.send_message = AsyncMock(side_effect=_send_message)
    bot.bot.edit_message_text = AsyncMock()
    bot.bot.delete_message = AsyncMock()
    bot.operation_presenter._debounce = 0
    try:
        yield bot
    finally:
        # A TelegramBot owns an aiohttp session, a bridge worker set and a
        # durable ledger connection; leaving them open leaks sockets and file
        # handles across the suite.
        await bot.close()


async def _start_presenter(bot: TelegramBot) -> None:
    if not bot.operation_presenter._running:
        await bot.dp.emit_startup()


async def _create_presented_operation(
    bot: TelegramBot,
    message: MagicMock,
) -> str:
    await _start_presenter(bot)
    operation = await bot.operation_store.create(
        chat_id=message.chat.id,
        user_id=message.from_user.id,
        text=message.text,
        message_id=message.message_id,
    )
    await bot.event_bus.publish(
        OperationReceived(
            operation_id=operation.id,
            chat_id=message.chat.id,
            user_id=message.from_user.id,
            text=message.text,
            message_id=message.message_id,
        )
    )
    await bot.event_bus.publish(
        StageChanged(
            operation_id=operation.id,
            stage=OperationState.CLASSIFYING.value,
            description="Анализирую запрос",
        )
    )
    return operation.id


def test_processing_result_defaults_fail_closed() -> None:
    result = ProcessingResult(
        success=True,
        task_id="flow-1",
        session_id="flow-1",
        response_text="accepted",
        error=None,
        correlation_id="corr",
        duration_ms=1.0,
    )

    assert result.outcome is ProcessingOutcome.FLOW_ACCEPTED
    assert result.terminal is False


@pytest.mark.parametrize("through_bot", [False, True], ids=["presenter", "bot-helper"])
@pytest.mark.asyncio
async def test_final_send_failure_never_manifests_terminal_or_deletes_progress(
    bot_instance: TelegramBot,
    through_bot: bool,
) -> None:
    message = _message("финал", message_id=40)
    operation_id = await _create_presented_operation(bot_instance, message)
    bot_instance.bot.send_message = AsyncMock(
        side_effect=RuntimeError("synthetic Telegram failure")
    )
    bot_instance.bot.delete_message.reset_mock()

    if through_bot:
        delivered = await bot_instance._publish_operation_final(
            operation_id,
            "готово",
            terminal_state=OperationState.SUCCEEDED,
        )
        assert delivered is False
    else:
        receipt = await bot_instance.operation_presenter.deliver_final(
            FinalResponseReady(
                operation_id=operation_id,
                text="готово",
                terminal_state=OperationState.SUCCEEDED.value,
            )
        )
        assert receipt is None

    persisted = await bot_instance.operation_store.get(operation_id)
    assert persisted is not None
    assert persisted.status == OperationState.CLASSIFYING.value
    assert persisted.final_message_ids == []
    bot_instance.bot.send_message.assert_awaited_once()
    bot_instance.bot.delete_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_manifest_failure_is_strict_at_most_once_without_terminal_cleanup(
    bot_instance: TelegramBot,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    message = _message("финал", message_id=50)
    operation_id = await _create_presented_operation(bot_instance, message)
    bot_instance.bot.send_message.reset_mock()
    bot_instance.bot.delete_message.reset_mock()
    manifest_write = AsyncMock(side_effect=RuntimeError("synthetic database failure"))
    monkeypatch.setattr(
        bot_instance.operation_store,
        "add_final_message_id",
        manifest_write,
    )
    event = FinalResponseReady(
        operation_id=operation_id,
        text="готово",
        terminal_state=OperationState.SUCCEEDED.value,
    )

    first = await bot_instance.operation_presenter.deliver_final(event)
    recovery_presenter = OperationPresenter(
        bot_instance.bot,
        bot_instance.event_bus,
        bot_instance.operation_store,
        edit_debounce_seconds=0,
    )
    second = await recovery_presenter.deliver_final(event)

    assert first is None
    assert second is None
    # Claim-before-send deliberately chooses strict at-most-once semantics:
    # the durable ambiguous claim blocks blind replay after the manifest crash.
    assert bot_instance.bot.send_message.await_count == 1
    manifest_write.assert_awaited_once()
    claim = await bot_instance.operation_store.claim_final_delivery(operation_id)
    assert claim.claimed is False
    assert claim.existing_message_id is None
    assert claim.in_flight is True

    persisted = await bot_instance.operation_store.get(operation_id)
    assert persisted is not None
    assert persisted.status == OperationState.CLASSIFYING.value
    assert persisted.final_message_ids == []
    assert persisted.progress_message_id is not None
    bot_instance.bot.delete_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_concurrent_duplicate_operation_received_sends_one_progress(
    bot_instance: TelegramBot,
) -> None:
    await _start_presenter(bot_instance)
    message = _message("одна задача", message_id=60)
    operation = await bot_instance.operation_store.create(
        chat_id=message.chat.id,
        user_id=message.from_user.id,
        text=message.text,
        message_id=message.message_id,
    )
    duplicates = [
        OperationReceived(
            operation_id=operation.id,
            chat_id=message.chat.id,
            user_id=message.from_user.id,
            text=message.text,
            message_id=message.message_id,
        )
        for _ in range(8)
    ]

    await asyncio.gather(
        *(bot_instance.event_bus.publish(event) for event in duplicates)
    )

    assert bot_instance.bot.send_message.await_count == 1
    persisted = await bot_instance.operation_store.get(operation.id)
    assert persisted is not None
    assert persisted.progress_message_id == 1001


@pytest.mark.asyncio
async def test_concurrent_duplicate_finals_share_receipt_and_cleanup_once(
    bot_instance: TelegramBot,
) -> None:
    message = _message("один финал", message_id=61)
    operation_id = await _create_presented_operation(bot_instance, message)
    bot_instance.bot.send_message.reset_mock()
    bot_instance.bot.delete_message.reset_mock()
    event = FinalResponseReady(
        operation_id=operation_id,
        text="подтверждённый результат",
        terminal_state=OperationState.SUCCEEDED.value,
    )

    receipts = await asyncio.gather(
        *(bot_instance.operation_presenter.deliver_final(event) for _ in range(8))
    )

    assert receipts == [1002] * 8
    assert bot_instance.bot.send_message.await_count == 1
    bot_instance.bot.delete_message.assert_awaited_once()
    persisted = await bot_instance.operation_store.get(operation_id)
    assert persisted is not None
    assert persisted.status == OperationState.SUCCEEDED.value
    assert persisted.final_message_ids == [1002]


@pytest.mark.asyncio
async def test_store_rejects_stale_cas_and_terminal_resurrection(
    bot_instance: TelegramBot,
) -> None:
    await bot_instance.startup()
    operation = await bot_instance.operation_store.create(
        chat_id=1,
        user_id=2,
        text="x",
        message_id=3,
    )
    assert await bot_instance.operation_store.transition_status(
        operation.id,
        OperationState.CLASSIFYING,
        expected_current=OperationState.RECEIVED,
    )
    assert not await bot_instance.operation_store.transition_status(
        operation.id,
        OperationState.RUNNING,
        expected_current=OperationState.RECEIVED,
    )
    assert await bot_instance.operation_store.transition_status(
        operation.id,
        OperationState.FINALIZING,
        expected_current=OperationState.CLASSIFYING,
    )
    assert await bot_instance.operation_store.transition_status(
        operation.id,
        OperationState.SUCCEEDED,
        expected_current=OperationState.FINALIZING,
    )

    assert not await bot_instance.operation_store.transition_status(
        operation.id,
        OperationState.WAITING_USER,
        expected_current=OperationState.SUCCEEDED,
    )
    persisted = await bot_instance.operation_store.get(operation.id)
    assert persisted is not None
    assert persisted.status == OperationState.SUCCEEDED.value


@pytest.mark.asyncio
async def test_reply_to_terminal_progress_creates_new_operation(
    bot_instance: TelegramBot,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = _message("исходная задача", chat_id=810, message_id=70)
    operation_id = await _create_presented_operation(bot_instance, original)
    stale = await bot_instance.operation_store.get(operation_id)
    assert stale is not None
    assert stale.progress_message_id is not None
    assert await bot_instance.operation_store.transition_status(
        operation_id,
        OperationState.FINALIZING,
        expected_current=OperationState.CLASSIFYING,
    )
    assert await bot_instance.operation_store.transition_status(
        operation_id,
        OperationState.SUCCEEDED,
        expected_current=OperationState.FINALIZING,
    )

    async def _turn(**kwargs):
        return {
            "reply": "Принято.",
            "session_id": "telegram:810",
            "response_type": "conversation",
            "flow_id": None,
            "requires_approval": False,
            "verified": None,
        }

    turn_mock = AsyncMock(side_effect=_turn)
    monkeypatch.setattr(bot_instance.gateway_client, "send_dialogue_turn", turn_mock)
    reply = _reply_message(
        "поздняя корректировка",
        chat_id=original.chat.id,
        message_id=71,
        reply_to_message_id=stale.progress_message_id,
    )

    await _text_handler(bot_instance)(reply)

    # Терминальная операция не активируется повторно: создаётся НОВАЯ
    # операция, текст уходит в ядро.
    turn_mock.assert_awaited_once()
    terminal = await bot_instance.operation_store.get(operation_id)
    assert terminal is not None
    assert terminal.status == OperationState.SUCCEEDED.value
    replacement = await bot_instance.operation_store.find_by_message_id(
        reply.chat.id,
        reply.message_id,
    )
    assert replacement is not None
    assert replacement.id != operation_id
    assert replacement.status in {
        OperationState.SUCCEEDED.value,
        OperationState.RUNNING.value,
    }

async def test_progress_stage_tool_and_final_escape_html_and_redact_secrets(
    bot_instance: TelegramBot,
) -> None:
    message = _message("безопасная задача", message_id=80)
    operation_id = await _create_presented_operation(bot_instance, message)
    bot_instance.bot.edit_message_text.reset_mock()
    malicious = (
        "OPENAI_API_KEY=synthetic-api-key "
        "<b>bold</b><script>alert(1)</script>"
    )

    await bot_instance._publish_operation_stage(
        operation_id,
        OperationState.RUNNING.value,
        description=malicious,
    )
    await bot_instance._publish_tool_progress(
        operation_id,
        tool_name="reader",
        status=malicious,
        preview=malicious,
    )
    delivered = await bot_instance._publish_operation_final(
        operation_id,
        malicious,
        terminal_state=OperationState.SUCCEEDED,
    )

    assert delivered is True
    edit_payloads = [
        call.kwargs["text"]
        for call in bot_instance.bot.edit_message_text.await_args_list
    ]
    final_payload = bot_instance.bot.send_message.await_args_list[-1].kwargs["text"]
    assert len(edit_payloads) == 2
    for payload in [*edit_payloads, final_payload]:
        assert "synthetic-api-key" not in payload
        assert "[REDACTED]" in payload
        assert "<b>bold</b>" not in payload
        assert "<script>" not in payload
        assert "&lt;b&gt;bold&lt;/b&gt;" in payload
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in payload


@pytest.mark.asyncio
async def test_task_failure_delivers_one_failure_final_then_deletes(
    bot_instance: TelegramBot,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Задача завершилась FAILED → ровно один failure final, progress удалён."""
    order: list[tuple[str, int]] = []
    receipts = iter((2001, 2002))
    add_final_message_id = bot_instance.operation_store.add_final_message_id

    async def _send(**kwargs):
        receipt = next(receipts)
        order.append(("send", receipt))
        return SimpleNamespace(message_id=receipt)

    async def _delete(**kwargs):
        order.append(("delete", kwargs["message_id"]))

    async def _manifest(operation_id: str, message_id: int) -> None:
        await add_final_message_id(operation_id, message_id)
        order.append(("manifest", message_id))

    bot_instance.bot.send_message = AsyncMock(side_effect=_send)
    bot_instance.bot.delete_message = AsyncMock(side_effect=_delete)
    monkeypatch.setattr(
        bot_instance.operation_store,
        "add_final_message_id",
        _manifest,
    )
    await _start_presenter(bot_instance)
    flow_id = "flow-fail-1"

    async def _turn(**kwargs):
        return {
            "reply": "Задача принята.",
            "session_id": "telegram:820",
            "response_type": "task_accepted",
            "flow_id": flow_id,
            "requires_approval": False,
            "verified": None,
        }

    class _Flow:
        status = FlowStatus.FAILED

    monkeypatch.setattr(bot_instance.gateway_client, "send_dialogue_turn", AsyncMock(side_effect=_turn))
    monkeypatch.setattr(bot_instance.gateway_client, "get_flow", AsyncMock(return_value=_Flow()))
    message = _message("выполни задачу", chat_id=820, message_id=90)

    await _text_handler(bot_instance)(message)

    send_texts = [
        call.kwargs["text"]
        for call in bot_instance.bot.send_message.await_args_list
    ]
    assert len([text for text in send_texts if text.startswith("🎯")]) == 1
    assert len([text for text in send_texts if text.startswith("❌")]) == 1
    assert not any(text.startswith("✅") for text in send_texts)
    message.answer.assert_not_awaited()

    operation = await bot_instance.operation_store.find_by_message_id(
        message.chat.id,
        message.message_id,
    )
    assert operation is not None
    assert operation.status == OperationState.FAILED.value
    assert operation.status != OperationState.SUCCEEDED.value
    bot_instance.bot.delete_message.assert_awaited()


async def test_final_send_without_receipt_never_commits_or_deletes(
    bot_instance: TelegramBot,
) -> None:
    message = _message("финал без receipt", message_id=100)
    operation_id = await _create_presented_operation(bot_instance, message)
    bot_instance.bot.send_message = AsyncMock(return_value=SimpleNamespace())
    bot_instance.bot.delete_message.reset_mock()

    delivered = await bot_instance._publish_operation_final(
        operation_id,
        "готово",
        terminal_state=OperationState.SUCCEEDED,
    )

    assert delivered is False
    persisted = await bot_instance.operation_store.get(operation_id)
    assert persisted is not None
    assert persisted.status == OperationState.CLASSIFYING.value
    assert persisted.final_message_ids == []
    bot_instance.bot.send_message.assert_awaited_once()
    bot_instance.bot.delete_message.assert_not_awaited()
    claim = await bot_instance.operation_store.claim_final_delivery(operation_id)
    assert claim.claimed is False
    assert claim.in_flight is True


@pytest.mark.asyncio
async def test_duplicate_final_after_manifest_reuses_receipt_without_side_effects(
    bot_instance: TelegramBot,
) -> None:
    message = _message("повтор финала", message_id=110)
    operation_id = await _create_presented_operation(bot_instance, message)
    bot_instance.bot.send_message.reset_mock()
    bot_instance.bot.delete_message.reset_mock()
    event = FinalResponseReady(
        operation_id=operation_id,
        text="готово",
        terminal_state=OperationState.SUCCEEDED.value,
    )

    first = await bot_instance.operation_presenter.deliver_final(event)
    second = await bot_instance.operation_presenter.deliver_final(event)

    assert first == 1002
    assert second == first
    assert bot_instance.bot.send_message.await_count == 1
    bot_instance.bot.delete_message.assert_awaited_once()
    persisted = await bot_instance.operation_store.get(operation_id)
    assert persisted is not None
    assert persisted.status == OperationState.SUCCEEDED.value
    assert persisted.final_message_ids == [first]
