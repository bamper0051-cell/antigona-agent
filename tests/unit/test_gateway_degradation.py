"""DAY 6 — Gateway degradation and circuit breaker tests.

Tests:
  1. GatewayCircuitBreaker state machine (CLOSED → OPEN → HALF_OPEN → CLOSED)
  2. In-memory deferred task queue
  3. Degraded notification on first task
  4. Flush sends queued tasks and clears queue
  5. Recovery detection flag
  6. Conversation works without gateway (greeting/identity not blocked)
  7. Task request with Gateway down → queued + degraded message
  8. Circuit breaker threshold and timeout
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import pytest_asyncio

from antigona.channels.telegram.bot import TelegramBot
from antigona.core.gateway_client import GatewayConnectionError
from antigona.gateway.circuit_breaker import (
    FAILURE_THRESHOLD,
    RECOVERY_TIMEOUT,
    DeferredTask,
    GatewayCircuitBreaker,
)
from antigona.security.owner_identity import OwnerIdentity

# ─── Circuit Breaker Unit Tests ─────────────────────────────────────────────


class TestGatewayCircuitBreaker:
    """GatewayCircuitBreaker state machine tests."""

    def test_initial_state_is_closed(self) -> None:
        cb = GatewayCircuitBreaker()
        assert cb.state == "CLOSED"
        assert cb.is_available() is True
        assert cb.failure_count == 0

    def test_after_one_failure_stays_closed(self) -> None:
        cb = GatewayCircuitBreaker()
        cb.record_failure()
        assert cb.state == "CLOSED"  # only 1 failure
        assert cb.failure_count == 1
        assert cb.is_available() is True

    def test_after_threshold_failures_transitions_to_open(self) -> None:
        cb = GatewayCircuitBreaker()
        for _ in range(FAILURE_THRESHOLD):
            cb.record_failure()
        assert cb.state == "OPEN"
        assert cb.failure_count == FAILURE_THRESHOLD
        assert cb.is_available() is False  # Not available while OPEN

    def test_open_state_blocks_requests(self) -> None:
        cb = GatewayCircuitBreaker()
        # Push to OPEN
        for _ in range(FAILURE_THRESHOLD):
            cb.record_failure()
        assert cb.state == "OPEN"

        # Immediately after, not available
        assert cb.is_available() is False

    def test_open_to_half_open_after_timeout(self) -> None:
        cb = GatewayCircuitBreaker()
        for _ in range(FAILURE_THRESHOLD):
            cb.record_failure()
        assert cb.state == "OPEN"

        # We cannot actually wait RECOVERY_TIMEOUT seconds in a unit test.
        # Instead, simulate by setting last_failure_time far in the past.
        cb.last_failure_time = time.monotonic() - RECOVERY_TIMEOUT - 1

        assert cb.is_available() is True  # HALF_OPEN transition
        assert cb.state == "HALF_OPEN"

    def test_half_open_to_closed_on_success(self) -> None:
        cb = GatewayCircuitBreaker()
        for _ in range(FAILURE_THRESHOLD):
            cb.record_failure()
        assert cb.state == "OPEN"

        # Simulate timeout and probe
        cb.last_failure_time = time.monotonic() - RECOVERY_TIMEOUT - 1
        assert cb.is_available() is True  # transitions to HALF_OPEN

        cb.record_success()
        assert cb.state == "CLOSED"
        assert cb.failure_count == 0

    def test_half_open_to_open_on_failure(self) -> None:
        cb = GatewayCircuitBreaker()
        for _ in range(FAILURE_THRESHOLD):
            cb.record_failure()
        cb.last_failure_time = time.monotonic() - RECOVERY_TIMEOUT - 1
        assert cb.is_available() is True  # HALF_OPEN
        cb.record_failure()  # fail during HALF_OPEN
        assert cb.state == "OPEN"

    def test_recovery_notification_flag(self) -> None:
        cb = GatewayCircuitBreaker()
        # Not recovered yet
        assert cb.consume_recovery_flag() is False

        # Simulate HALF_OPEN → CLOSED transition
        cb.state = "HALF_OPEN"
        cb.record_success()
        assert cb.state == "CLOSED"
        assert cb.consume_recovery_flag() is True  # One-time flag
        assert cb.consume_recovery_flag() is False  # Cleared after consume

    def test_record_success_resets_everything(self) -> None:
        cb = GatewayCircuitBreaker()
        for _ in range(2):
            cb.record_failure()
        cb.record_success()
        assert cb.failure_count == 0
        assert cb.state == "CLOSED"
        assert cb.is_available() is True


# ─── Deferred Task Queue Tests ──────────────────────────────────────────────


class TestDeferredTaskQueue:
    """In-memory deferred task queue tests."""

    def test_enqueue_and_count(self) -> None:
        cb = GatewayCircuitBreaker()
        assert cb.queued_task_count == 0

        task = DeferredTask(goal="test", path="/tmp/x", content="x", tool_name="tool", command=[])
        cb.enqueue_task(task)
        assert cb.queued_task_count == 1

    def test_flush_returns_all_and_clears(self) -> None:
        cb = GatewayCircuitBreaker()
        t1 = DeferredTask(goal="g1", path="p1", content="c1", tool_name="t1", command=[])
        t2 = DeferredTask(goal="g2", path="p2", content="c2", tool_name="t2", command=[])
        cb.enqueue_task(t1)
        cb.enqueue_task(t2)

        tasks = cb.flush_tasks()
        assert len(tasks) == 2
        assert tasks[0].goal == "g1"
        assert tasks[1].goal == "g2"
        assert cb.queued_task_count == 0

    def test_flush_empty_returns_empty_list(self) -> None:
        cb = GatewayCircuitBreaker()
        assert cb.flush_tasks() == []
        assert cb.queued_task_count == 0

    def test_degraded_notification_flag(self) -> None:
        cb = GatewayCircuitBreaker()
        assert cb._degraded_notified is False

        cb.record_failure()
        cb.record_failure()
        cb.record_failure()  # State is OPEN

        cb._degraded_notified = True
        # After success, flag resets
        cb.state = "HALF_OPEN"
        cb.record_success()
        assert cb._degraded_notified is False


# ─── Helpers: invoke the real plain-text/task handler ───────────────────────


def _message(text: str, *, chat_id: int = 700, message_id: int = 1) -> MagicMock:
    """Build a mock aiogram.types.Message for direct handler invocation."""
    msg = MagicMock()
    msg.text = text
    msg.chat = MagicMock()
    msg.chat.id = chat_id
    msg.chat.type = "private"
    msg.message_id = message_id
    msg.from_user = MagicMock()
    msg.from_user.id = 701
    msg.from_user.is_bot = False
    msg.reply_to_message = None
    msg.bot = MagicMock()
    msg.bot.send_chat_action = AsyncMock()
    msg.answer = AsyncMock(return_value=MagicMock(message_id=900))
    return msg


def _text_handler(bot: TelegramBot):
    """The real plain-text/task handler is the last-registered F.text handler.

    It must be located by name, not by index: many command handlers were
    added after this test file was written, so ``handlers[3]`` is no longer
    the text handler (it is the /agent command handler).
    """
    return next(
        handler.callback
        for handler in bot.router.message.handlers
        if handler.callback.__name__ == "text_handler"
    )


async def _start_presenter(bot: TelegramBot) -> None:
    """Start the OperationPresenter event subscriptions if not already running."""
    if not bot.operation_presenter._running:
        await bot.dp.emit_startup()


def _presenter_texts(bot: TelegramBot) -> list[str]:
    """Return every presenter-delivered (bot.send_message) reply text."""
    return [call.kwargs["text"] for call in bot.bot.send_message.await_args_list]


async def _await_presenter_texts(
    bot: TelegramBot,
    predicate: Callable[[list[str]], bool],
    timeout: float = 5.0,
) -> list[str]:
    """Poll the presenter's delivered texts until *predicate* holds.

    Presenter delivery is event-driven (async); under a loaded full-suite run
    the text may not be present immediately after the handler returns, so we
    wait (bounded) instead of asserting on a snapshot that may race.
    """
    deadline = time.monotonic() + timeout
    while True:
        texts = _presenter_texts(bot)
        if bool(predicate(texts)):
            return texts
        if time.monotonic() >= deadline:
            return texts
        await asyncio.sleep(0.05)


@pytest_asyncio.fixture
async def bot_instance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TelegramBot:
    """Real TelegramBot (sqlite) with no live Telegram/Gateway/LLM calls.

    Presenter message delivery is mocked, the LLM fallback (chitchat_reply)
    is stubbed to a fixed string, and the owner gate accepts every test user.
    """
    monkeypatch.setenv("ANTIGONA_ENABLE_STARTUP_RECOVERY", "0")
    monkeypatch.setattr(OwnerIdentity, "is_owner", lambda self, user_id: True)

    import antigona.channels.telegram.bot as bot_mod

    monkeypatch.setattr(
        bot_mod, "chitchat_reply", lambda *args, **kwargs: "Привет! Чем могу помочь?"
    )

    bot = TelegramBot(
        token="123456789:ABCdefGHIjklMNOpqrsTUVwxyz",
        gateway_url="http://test-gateway:8090",
        gateway_token="test-token",
        database_url=f"sqlite:///{tmp_path / 'gateway.db'}",
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
        await bot.close()


# ─── Conversation Works Without Gateway ─────────────────────────────────────


# ─── Conversation Through Gateway ──────────────────────────────────────────


class TestConversationThroughGateway:
    """Step 4: весь текст уходит в Gateway Turn API; ядро решает: разговор или задача."""

    @pytest.mark.asyncio
    async def test_greeting_routes_through_gateway_turn(self, bot_instance: TelegramBot) -> None:
        """'Привет' → Turn API → conversation-ответ ядра (submit не используется)."""
        await _start_presenter(bot_instance)

        async def _turn(**kwargs):
            return {
                "reply": "Привет! Чем могу помочь?",
                "session_id": "telegram:12345",
                "response_type": "conversation",
                "flow_id": None,
                "requires_approval": False,
                "verified": None,
            }

        bot_instance.gateway_client.submit = AsyncMock(
            side_effect=AssertionError("gateway.submit must not be called for chitchat")
        )
        bot_instance.gateway_client.send_dialogue_turn = AsyncMock(side_effect=_turn)
        message = _message("Привет")

        await _text_handler(bot_instance)(message)

        bot_instance.gateway_client.submit.assert_not_awaited()
        bot_instance.gateway_client.send_dialogue_turn.assert_awaited_once()
        texts = await _await_presenter_texts(
            bot_instance, lambda ts: any("Чем могу помочь" in t for t in ts)
        )
        assert any("Чем могу помочь" in t for t in texts)

    @pytest.mark.asyncio
    async def test_identity_routes_through_gateway_turn(self, bot_instance: TelegramBot) -> None:
        """'Кто ты?' — разговор через ядро."""
        await _start_presenter(bot_instance)

        async def _turn(**kwargs):
            return {
                "reply": "Я Antigona.",
                "session_id": "telegram:12345",
                "response_type": "conversation",
                "flow_id": None,
                "requires_approval": False,
                "verified": None,
            }

        bot_instance.gateway_client.submit = AsyncMock(
            side_effect=AssertionError("gateway.submit must not be called for chitchat")
        )
        bot_instance.gateway_client.send_dialogue_turn = AsyncMock(side_effect=_turn)
        message = _message("Кто ты?")

        await _text_handler(bot_instance)(message)

        bot_instance.gateway_client.submit.assert_not_awaited()
        bot_instance.gateway_client.send_dialogue_turn.assert_awaited_once()
        texts = await _await_presenter_texts(
            bot_instance, lambda ts: any("Antigona" in t for t in ts)
        )
        assert any("Antigona" in t for t in texts)

    @pytest.mark.asyncio
    async def test_noise_routes_through_gateway_turn(self, bot_instance: TelegramBot) -> None:
        """'?' — разговор через ядро (ядро классифицирует noise)."""
        await _start_presenter(bot_instance)

        async def _turn(**kwargs):
            return {
                "reply": "Ты написал просто знак вопроса.",
                "session_id": "telegram:12345",
                "response_type": "conversation",
                "flow_id": None,
                "requires_approval": False,
                "verified": None,
            }

        bot_instance.gateway_client.submit = AsyncMock(
            side_effect=AssertionError("gateway.submit must not be called for chitchat")
        )
        bot_instance.gateway_client.send_dialogue_turn = AsyncMock(side_effect=_turn)
        message = _message("?")

        await _text_handler(bot_instance)(message)

        bot_instance.gateway_client.submit.assert_not_awaited()
        bot_instance.gateway_client.send_dialogue_turn.assert_awaited_once()
        texts = await _await_presenter_texts(
            bot_instance, lambda ts: any("знак вопроса" in t for t in ts)
        )
        assert any("знак вопроса" in t for t in texts)


# ─── Task Request With Gateway Down ─────────────────────────────────────────


class TestTaskWhenGatewayDown:
    """Task requests degrade gracefully when Gateway is unavailable."""

    @pytest.mark.asyncio
    async def test_task_degrades_when_gateway_unavailable(
        self, bot_instance: TelegramBot
    ) -> None:
        """Gateway down → честное 'недоступен' через presenter (fail-closed)."""
        await _start_presenter(bot_instance)

        async def _down(*args: object, **kwargs: object) -> object:
            raise GatewayConnectionError("gateway down")

        bot_instance.gateway_client.send_dialogue_turn = AsyncMock(side_effect=_down)
        message = _message("создай файл test.txt с содержимым hello")

        await _text_handler(bot_instance)(message)

        bot_instance.gateway_client.send_dialogue_turn.assert_awaited_once()
        texts = await _await_presenter_texts(
            bot_instance, lambda ts: any("недоступен" in t.lower() for t in ts)
        )
        assert any("недоступен" in t.lower() for t in texts)

    @pytest.mark.asyncio
    async def test_repeated_tasks_degrade_gracefully(
        self, bot_instance: TelegramBot
    ) -> None:
        """Two consecutive gateway-down tasks both degrade, never crash."""
        await _start_presenter(bot_instance)

        async def _down(*args: object, **kwargs: object) -> object:
            raise GatewayConnectionError("gateway down")

        bot_instance.gateway_client.send_dialogue_turn = AsyncMock(side_effect=_down)

        await _text_handler(bot_instance)(_message("напиши скрипт deploy.sh"))
        await _text_handler(bot_instance)(_message("напиши скрипт deploy.sh"))

        assert bot_instance.gateway_client.send_dialogue_turn.await_count == 2
        texts = await _await_presenter_texts(
            bot_instance, lambda ts: sum("недоступен" in t.lower() for t in ts) >= 2
        )
        assert sum("недоступен" in t.lower() for t in texts) == 2

    @pytest.mark.asyncio
    async def test_task_degrades_when_connection_error(
        self, bot_instance: TelegramBot
    ) -> None:
        """A connection-level error while submitting also degrades gracefully."""
        await _start_presenter(bot_instance)

        async def _connect_error(*args: object, **kwargs: object) -> object:
            raise httpx.ConnectError("Connection refused", request=httpx.Request("POST", "http://x"))

        bot_instance.gateway_client.send_dialogue_turn = AsyncMock(side_effect=_connect_error)
        message = _message("создай файл hello.txt")

        await _text_handler(bot_instance)(message)

        bot_instance.gateway_client.send_dialogue_turn.assert_awaited_once()
        texts = await _await_presenter_texts(
            bot_instance, lambda ts: any("недоступен" in t.lower() for t in ts)
        )
        # Graceful degradation: a terminal reply is delivered, no exception escapes.
        assert any("недоступен" in t.lower() for t in texts)

    @pytest.mark.asyncio
    async def test_gateway_down_answers_honestly_for_conversation(
        self, bot_instance: TelegramBot
    ) -> None:
        """Step 4 fail-closed: без ядра интерфейс не отвечает на разговор —
        честно сообщает, что Gateway недоступен (никакого локального движка)."""
        await _start_presenter(bot_instance)

        async def _down(*args: object, **kwargs: object) -> object:
            raise GatewayConnectionError("gateway down")

        bot_instance.gateway_client.send_dialogue_turn = AsyncMock(side_effect=_down)

        await _text_handler(bot_instance)(_message("Привет"))
        bot_instance.gateway_client.send_dialogue_turn.assert_awaited_once()
        texts = await _await_presenter_texts(
            bot_instance, lambda ts: any("недоступен" in t.lower() for t in ts)
        )
        assert any("недоступен" in t.lower() for t in texts)


class TestGatewayRecovery:
    """Verify tasks are flushed when Gateway comes back up."""

    @pytest.mark.asyncio
    async def test_flush_deferred_tasks_sends_all_queued(self) -> None:
        """_flush_deferred_queue sends all queued tasks via post_flow."""
        bot = TelegramBot(
            token="123456789:ABCdefGHIjklMNOpqrsTUVwxyz",
            gateway_url="http://test-gateway:8090",
            gateway_token="test-token",
        )

        # Queue some tasks
        t1 = DeferredTask(goal="task1", path="/tmp/a", content="a", tool_name="tool", command=[])
        t2 = DeferredTask(goal="task2", path="/tmp/b", content="b", tool_name="tool", command=[])
        bot.circuit_breaker.enqueue_task(t1)
        bot.circuit_breaker.enqueue_task(t2)
        assert bot.circuit_breaker.queued_task_count == 2

        with patch.object(bot, "post_flow", new=AsyncMock()) as mock_post:
            await bot._flush_deferred_queue()

            # post_flow called for each task
            assert mock_post.await_count == 2
            assert bot.circuit_breaker.queued_task_count == 0

    @pytest.mark.asyncio
    async def test_flush_empty_queue_does_nothing(self) -> None:
        """_flush_deferred_queue on empty queue does not call post_flow."""
        bot = TelegramBot(
            token="123456789:ABCdefGHIjklMNOpqrsTUVwxyz",
            gateway_url="http://test-gateway:8090",
            gateway_token="test-token",
        )
        assert bot.circuit_breaker.queued_task_count == 0

        with patch.object(bot, "post_flow", new=AsyncMock()) as mock_post:
            await bot._flush_deferred_queue()
            mock_post.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_recovery_notification_after_flush(self) -> None:
        """After recovery + flush, recovery flag is set."""
        cb = GatewayCircuitBreaker()
        # Get to OPEN, then simulate timeout
        for _ in range(FAILURE_THRESHOLD):
            cb.record_failure()
        cb.last_failure_time = time.monotonic() - RECOVERY_TIMEOUT - 1
        assert cb.is_available() is True  # HALF_OPEN

        cb.record_success()
        assert cb.state == "CLOSED"
        assert cb.consume_recovery_flag() is True

    @pytest.mark.asyncio
    async def test_health_probe_on_recovery(self) -> None:
        """Recovery logic properly handles HALF_OPEN → CLOSED transition with queued tasks."""
        bot = TelegramBot(
            token="123456789:ABCdefGHIjklMNOpqrsTUVwxyz",
            gateway_url="http://test-gateway:8090",
            gateway_token="test-token",
        )

        # Force OPEN
        cb = bot.circuit_breaker
        for _ in range(FAILURE_THRESHOLD):
            cb.record_failure()
        assert cb.state == "OPEN"

        # Queue a task
        cb.enqueue_task(
            DeferredTask(goal="test", path="/tmp/x", content="x", tool_name="tool", command=[])
        )
        assert cb.queued_task_count == 1

        # Simulate what happens after _recovery_loop detects health=200:
        # 1. record_success is called → HALF_OPEN → CLOSED
        # 2. recovery flag is set
        # 3. _flush_deferred_queue is called
        with patch.object(bot, "_flush_deferred_queue", new=AsyncMock()):
            # Simulate the recovery path
            cb._degraded_notified = True
            cb.state = "HALF_OPEN"
            cb.record_success()

            assert cb.state == "CLOSED"
            assert cb.consume_recovery_flag() is True

            # If we called flush, queue would be empty
            # Since we mocked flush, tasks still in queue
            assert cb.queued_task_count == 1


# ─── GatewayCircuitBreaker Edge Cases ────────────────────────────────────────


class TestCircuitBreakerEdgeCases:
    """Edge cases for the circuit breaker."""

    def test_no_recovery_flag_without_half_open(self) -> None:
        """consume_recovery_flag returns False when no HALF_OPEN→CLOSED transition."""
        cb = GatewayCircuitBreaker()
        assert cb.consume_recovery_flag() is False
        cb.record_success()  # CLOSED → CLOSED
        assert cb.consume_recovery_flag() is False

    def test_is_closed_and_is_open_helpers(self) -> None:
        cb = GatewayCircuitBreaker()
        assert cb.is_closed() is True
        assert cb.is_open() is False

        for _ in range(FAILURE_THRESHOLD):
            cb.record_failure()
        assert cb.is_closed() is False
        assert cb.is_open() is True

    def test_is_closed_after_successful_recovery(self) -> None:
        cb = GatewayCircuitBreaker()
        for _ in range(FAILURE_THRESHOLD):
            cb.record_failure()
        cb.last_failure_time = time.monotonic() - RECOVERY_TIMEOUT - 1
        cb.is_available()  # HALF_OPEN
        cb.record_success()
        assert cb.is_closed() is True

    def test_deferred_task_fields(self) -> None:
        """DeferredTask dataclass stores all required fields."""
        task = DeferredTask(
            goal="create file x",
            path="/tmp/x.txt",
            content="hello world",
            tool_name="workspace.write_text",
            command=["sh", "-c", "echo hi"],
        )
        assert task.goal == "create file x"
        assert task.path == "/tmp/x.txt"
        assert task.content == "hello world"
        assert task.tool_name == "workspace.write_text"
        assert task.command == ["sh", "-c", "echo hi"]

    def test_degraded_notification_reset_on_success(self) -> None:
        cb = GatewayCircuitBreaker()
        cb._degraded_notified = True
        cb.record_success()
        assert cb._degraded_notified is False

    def test_open_state_blocks_until_timeout_passes(self) -> None:
        """is_available returns False until RECOVERY_TIMEOUT has elapsed."""
        cb = GatewayCircuitBreaker()
        for _ in range(FAILURE_THRESHOLD):
            cb.record_failure()
        assert cb.state == "OPEN"
        assert cb.is_available() is False  # blocked

        cb.last_failure_time = time.monotonic() - RECOVERY_TIMEOUT - 1
        assert cb.is_available() is True  # allowed after timeout
