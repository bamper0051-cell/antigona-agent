"""Integration tests for the Telegram operation presentation lifecycle.

The tests exercise the real typed EventBus, OperationStore, OperationPresenter,
and TelegramBot action handlers while replacing Telegram/Gateway/network calls
with mocks.  They protect the single-owner invariant:

* one presenter-owned progress bubble;
* progress is edited in place;
* exactly one final response for terminal work;
* the progress bubble is deleted only after delivery confirmation;
* delivered successful operations leave the active-operation index.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from antigona.channels.telegram.bot import TelegramBot
from antigona.core.control_plane import FlowStatus
from antigona.durable.operation_models import OperationState
from antigona.events.event_types import (
    OperationReceived,
    StageChanged,
)
from antigona.schemas import FlowResultView, VerifiedArtifactResultView
from antigona.security.owner_identity import OwnerIdentity


def _make_message(text: str, chat_id: int = 12345, message_id: int = 1) -> MagicMock:
    msg = MagicMock()
    msg.text = text
    msg.chat = MagicMock()
    msg.chat.id = chat_id
    msg.chat.type = "private"
    msg.message_id = message_id
    msg.from_user = MagicMock()
    msg.from_user.id = 99999
    msg.from_user.is_bot = False
    msg.reply_to_message = None
    msg.bot = MagicMock()
    msg.bot.send_chat_action = AsyncMock()
    msg.answer = AsyncMock(return_value=MagicMock(message_id=555))
    return msg


def _find_text_handler(bot: TelegramBot):
    for handler in bot.router.message.handlers:
        if handler.callback.__name__ == "text_handler":
            return handler.callback
    raise AssertionError("text_handler not registered on router.message")


async def _create_presented_operation(
    bot: TelegramBot,
    message: MagicMock,
) -> str:
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
            stage="CLASSIFYING",
            description="Анализирую запрос",
        )
    )
    return operation.id


@pytest_asyncio.fixture
async def bot_instance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TelegramBot:
    monkeypatch.setenv("ANTIGONA_ENABLE_STARTUP_RECOVERY", "0")
    # The conversation/action lifecycle probes exercise the durable presenter
    # machinery, not the owner gate.  Accept every test user so the probes are
    # independent of any ANTIGONA_OWNER_ID configured in the ambient environment.
    monkeypatch.setattr(OwnerIdentity, "is_owner", lambda self, user_id: True)
    db_url = f"sqlite:///{tmp_path / 'test_operations.db'}"
    bot = TelegramBot(
        token="123456789:ABCdefGHIjklMNOpqrsTUVwxyz",
        gateway_url="http://test-gateway:8090",
        gateway_token="test-token",
        database_url=db_url,
    )
    # Never hit a real Telegram API from this process.
    bot.bot.send_message = AsyncMock(
        side_effect=lambda **kw: MagicMock(
            message_id=abs(hash(kw.get("text", ""))) % 100000 + 1
        )
    )
    bot.bot.edit_message_text = AsyncMock()
    bot.bot.delete_message = AsyncMock()
    bot.operation_presenter._debounce = 0
    try:
        yield bot
    finally:
        await bot.close()


class TestCreateAllOnFreshDatabase:
    @pytest.mark.asyncio
    async def test_operations_table_exists_without_external_create_all(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ANTIGONA_ENABLE_STARTUP_RECOVERY", "0")
        db_url = f"sqlite:///{tmp_path / 'fresh.db'}"
        bot = TelegramBot(
            token="123456789:ABCdefGHIjklMNOpqrsTUVwxyz",
            gateway_url="http://test-gateway:8090",
            gateway_token="test-token",
            database_url=db_url,
        )
        await bot.startup()
        op = await bot.operation_store.create(
            chat_id=1,
            user_id=2,
            text="hello",
            message_id=1,
        )
        assert op.status == OperationState.RECEIVED.value


class TestDispatcherStartupHook:
    @pytest.mark.asyncio
    async def test_presenter_starts_only_from_dispatcher_startup(
        self, bot_instance: TelegramBot
    ) -> None:
        # Constructor must not schedule OperationPresenter.start() on a throwaway
        # event loop.  The dispatcher hook is its sole owner.
        assert bot_instance.operation_presenter._running is False
        assert not hasattr(bot_instance, "_start_presenter_task")

        real_start = bot_instance.operation_presenter.start
        start_spy = AsyncMock(side_effect=real_start)
        bot_instance.operation_presenter.start = start_spy

        start_spy.assert_not_awaited()
        await bot_instance.dp.emit_startup()

        start_spy.assert_awaited_once_with()
        assert bot_instance.operation_presenter._running is True


class TestFullOperationRoundTrip:
    @pytest.mark.asyncio
    async def test_conversation_has_one_progress_one_final_and_terminal_state(
        self, bot_instance: TelegramBot, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Разговор через Turn API: ровно один progress и один final (presenter)."""
        await bot_instance.dp.emit_startup()

        async def _turn(**kwargs):
            return {
                "reply": "Привет! Я Антигона.",
                "session_id": "telegram:12345",
                "response_type": "conversation",
                "flow_id": None,
                "requires_approval": False,
                "verified": None,
            }

        monkeypatch.setattr(bot_instance.gateway_client, "send_dialogue_turn", _turn)

        delivery_order: list[str] = []
        cast(AsyncMock, bot_instance.bot.edit_message_text).side_effect = (
            lambda **kwargs: delivery_order.append("edit")
        )
        cast(AsyncMock, bot_instance.bot.delete_message).side_effect = (
            lambda **kwargs: delivery_order.append("delete")
        )

        message = _make_message("Привет")
        await _find_text_handler(bot_instance)(message)

        # OperationPresenter — единственный владелец Telegram-доставки.
        send_texts = [
            call.kwargs["text"]
            for call in bot_instance.bot.send_message.await_args_list
        ]
        progress_texts = [text for text in send_texts if text.startswith("🎯")]
        # Finding D: a plain conversation reply completes no real action, so it
        # must NOT carry a "✅ Готово" completion header.
        assert not any(text.startswith("✅") for text in send_texts)
        final_texts = [text for text in send_texts if "Привет! Я Антигона." in text]
        assert len(progress_texts) == 1
        assert len(final_texts) == 1
        assert len(send_texts) == 2
        message.answer.assert_not_awaited()
        assert bot_instance.bot.edit_message_text.await_count >= 1
        bot_instance.bot.delete_message.assert_awaited_once()
        assert delivery_order.index("edit") < delivery_order.index("delete")

        operation = await bot_instance.operation_store.find_by_message_id(
            message.chat.id, message.message_id
        )
        assert operation is not None
        assert operation.status == OperationState.SUCCEEDED.value
        assert operation.progress_message_id is not None
        assert len(operation.final_message_ids or []) == 1
        assert await bot_instance.operation_store.find_active_by_chat(message.chat.id) is None

    @pytest.mark.asyncio
    async def test_task_accepted_done_has_one_progress_one_final(
        self, bot_instance: TelegramBot, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Задача через Turn API: task_accepted → поллинг DONE → один final."""
        await bot_instance.dp.emit_startup()

        flow_id = "flow-abc-123"

        async def _turn(**kwargs):
            return {
                "reply": "Задача принята и выполняется.",
                "session_id": "telegram:12345",
                "response_type": "task_accepted",
                "flow_id": flow_id,
                "requires_approval": False,
                "verified": None,
            }

        class _Flow:
            status = FlowStatus.DONE

        result_view = FlowResultView(
            flow_id=flow_id,
            status=FlowStatus.DONE.value,
            terminal=True,
            success=True,
            artifacts=[
                VerifiedArtifactResultView(
                    path="hello.txt",
                    sha256="a" * 64,
                    size=12,
                    verified=True,
                )
            ],
            safe_result_text="Готово: файл создан",
            stdout_preview=None,
            revision=1,
        )

        monkeypatch.setattr(bot_instance.gateway_client, "send_dialogue_turn", _turn)
        monkeypatch.setattr(
            bot_instance.gateway_client, "get_flow", AsyncMock(return_value=_Flow())
        )
        monkeypatch.setattr(
            bot_instance.gateway_client, "get_result", AsyncMock(return_value=result_view)
        )

        message = _make_message("создай файл hello.txt")
        await _find_text_handler(bot_instance)(message)

        send_texts = [
            call.kwargs["text"]
            for call in bot_instance.bot.send_message.await_args_list
        ]
        progress_texts = [text for text in send_texts if text.startswith("🎯")]
        final_texts = [text for text in send_texts if text.startswith("✅")]
        assert len(progress_texts) == 1
        assert len(final_texts) == 1
        assert "файл создан" in final_texts[0]
        message.answer.assert_not_awaited()

        operation = await bot_instance.operation_store.find_by_message_id(
            message.chat.id, message.message_id
        )
        assert operation is not None
        assert operation.status == OperationState.SUCCEEDED.value
        assert await bot_instance.operation_store.find_active_by_chat(message.chat.id) is None

    @pytest.mark.asyncio
    async def test_task_failed_delivers_failure_final(
        self, bot_instance: TelegramBot, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Задача не выполнилась: FAILED-статус → failure final, не success."""
        await bot_instance.dp.emit_startup()

        flow_id = "flow-fail-456"

        async def _turn(**kwargs):
            return {
                "reply": "Задача принята.",
                "session_id": "telegram:12345",
                "response_type": "task_accepted",
                "flow_id": flow_id,
                "requires_approval": False,
                "verified": None,
            }

        class _Flow:
            status = FlowStatus.FAILED

        monkeypatch.setattr(bot_instance.gateway_client, "send_dialogue_turn", _turn)
        monkeypatch.setattr(
            bot_instance.gateway_client, "get_flow", AsyncMock(return_value=_Flow())
        )

        message = _make_message("сделай невозможное")
        await _find_text_handler(bot_instance)(message)

        send_texts = [
            call.kwargs["text"]
            for call in bot_instance.bot.send_message.await_args_list
        ]
        final_texts = [text for text in send_texts if text.startswith("❌")]
        assert len(final_texts) == 1
        assert not any(text.startswith("✅") for text in send_texts)
        message.answer.assert_not_awaited()

        operation = await bot_instance.operation_store.find_by_message_id(
            message.chat.id, message.message_id
        )
        assert operation is not None
        assert operation.status == OperationState.FAILED.value
        assert await bot_instance.operation_store.find_active_by_chat(message.chat.id) is None


