"""P1 (plan2.md Phase 3) — false-success contract regression tests.

3.1: "document" must not be treated as the vague English verb "do".
A concrete file-write request in English ("Create document txt xxxx.md") must
route to task.file_write, never be short-circuited to clarify/followup.

3.3/3.4: an empty gateway reply is NEVER success — it must end as an explicit
non-success terminal, not a fabricated "✅ Готово.".
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from antigona.channels.telegram.bot import TelegramBot
from antigona.durable.operation_models import OperationState
from antigona.router.intent_router import IntentRouter


@pytest.fixture()
def router() -> IntentRouter:
    return IntentRouter()


@pytest.mark.parametrize(
    "text",
    [
        "Create document txt xxxx.md tekst: ggbb",
        "create document xxxx.md with hello",
        "Create a document named notes.md containing hi",
    ],
)
def test_english_file_write_is_not_vague_do(router: IntentRouter, text: str) -> None:
    """'document' contains 'do'; the vague-action regex must not match inside it."""
    decision = router.route(text=text, context={"source": "cli"})
    assert decision.intent == "task.file_write", f"got {decision.intent}"
    assert decision.entities.get("path"), f"entities: {decision.entities}"


def _text_handler(bot: TelegramBot):
    return next(
        h.callback
        for h in bot.router.message.handlers
        if h.callback.__name__ == "text_handler"
    )


def _make_bot_message(text: str) -> MagicMock:
    msg = MagicMock()
    msg.text = text
    msg.chat = MagicMock(); msg.chat.id = 12345; msg.chat.type = "private"
    msg.message_id = 1
    msg.from_user = MagicMock(); msg.from_user.id = 99999
    msg.from_user.is_bot = False
    msg.reply_to_message = None
    msg.bot = AsyncMock(); msg.bot.send_chat_action = AsyncMock()
    msg.answer = AsyncMock(); msg.answer.__name__ = "answer"
    msg.html_text = text
    return msg


@pytest.mark.asyncio
async def test_empty_reply_is_not_fake_success(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    """A gateway turn that returns an EMPTY reply must end as FAILED, never "✅ Готово."."""
    from antigona.security.owner_identity import OwnerIdentity

    monkeypatch.setenv("ANTIGONA_ENABLE_STARTUP_RECOVERY", "0")
    monkeypatch.setattr(OwnerIdentity, "is_owner", lambda self, user_id: True)
    bot = TelegramBot(
        token="123456789:ABCdefGHIjklMNOpqrsTUVwxyz",
        gateway_url="http://test-gateway:8090",
        gateway_token="test-token",
        database_url=f"sqlite:///{tmp_path / 'bot.db'}",
    )
    bot.bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=900))
    bot.operation_presenter._debounce = 0
    bot.event_bus.publish = AsyncMock()
    bot._store_session_message = AsyncMock()
    bot._update_last_response_time = MagicMock()
    bot._handle_plan_or_actions = AsyncMock(return_value=None)
    bot._operation_final_receipt = AsyncMock(return_value=None)
    bot._publish_operation_stage = AsyncMock()
    bot._publish_operation_final = AsyncMock(return_value=False)
    try:
        turn_mock = AsyncMock(return_value={
            "reply": "",
            "session_id": "telegram:12345",
            "response_type": "conversation",
            "flow_id": None,
            "requires_approval": False,
            "verified": None,
        })
        monkeypatch.setattr(bot.gateway_client, "send_dialogue_turn", turn_mock)
        await _text_handler(bot)(_make_bot_message("привет"))
    finally:
        await bot.close()
        await bot.gateway_client.close()

    calls = [c for c in bot._publish_operation_final.call_args_list]
    assert calls, "final publish should be called"
    final_text = calls[-1].args[1] if len(calls[-1].args) > 1 else str(calls[-1].kwargs.get("text", ""))
    terminal_state = calls[-1].kwargs.get("terminal_state")
    assert "✅ Готово" not in final_text, f"empty reply must not fake success: {final_text!r}"
    assert str(terminal_state) in ("FAILED", "ERROR", OperationState.FAILED.value), (
        f"empty reply must be a non-success terminal: {terminal_state}"
    )


# ── Phase 4: verified-success invariant (technical execution != verified success) ──


async def _run_turn_with(bot, monkeypatch, response_type, reply, verified):
    """Drive one text_handler turn and return the final publish call."""
    turn_mock = AsyncMock(return_value={
        "reply": reply,
        "session_id": "telegram:12345",
        "response_type": response_type,
        "flow_id": None,
        "requires_approval": False,
        "verified": verified,
    })
    monkeypatch.setattr(bot.gateway_client, "send_dialogue_turn", turn_mock)
    await _text_handler(bot)(_make_bot_message("создай файл x.txt"))
    calls = [c for c in bot._publish_operation_final.call_args_list]
    assert calls, "final publish should be called"
    return calls[-1]


@pytest.mark.asyncio
@pytest.mark.parametrize("verified", [False, None])
async def test_task_result_not_verified_is_not_success(
    monkeypatch: pytest.MonkeyPatch, tmp_path, verified,
) -> None:
    """A TASK_RESULT that the core did NOT confirm verified must end non-success."""
    from antigona.security.owner_identity import OwnerIdentity

    monkeypatch.setenv("ANTIGONA_ENABLE_STARTUP_RECOVERY", "0")
    monkeypatch.setattr(OwnerIdentity, "is_owner", lambda self, user_id: True)
    bot = TelegramBot(
        token="123456789:ABCdefGHIjklMNOpqrsTUVwxyz",
        gateway_url="http://test-gateway:8090",
        gateway_token="test-token",
        database_url=f"sqlite:///{tmp_path / 'bot.db'}",
    )
    bot.bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=900))
    bot.operation_presenter._debounce = 0
    bot.event_bus.publish = AsyncMock()
    bot._store_session_message = AsyncMock()
    bot._update_last_response_time = MagicMock()
    bot._handle_plan_or_actions = AsyncMock(return_value=None)
    bot._operation_final_receipt = AsyncMock(return_value=None)
    bot._publish_operation_stage = AsyncMock()
    bot._publish_operation_final = AsyncMock(return_value=False)
    try:
        call = await _run_turn_with(bot, monkeypatch, "TASK_RESULT", "сделано", verified)
    finally:
        await bot.close()
        await bot.gateway_client.close()
    state = call.kwargs.get("terminal_state")
    assert str(state) in ("FAILED", OperationState.FAILED.value), f"got {state}"


@pytest.mark.asyncio
async def test_task_result_verified_is_success(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    """A TASK_RESULT confirmed verified=True IS a success."""
    from antigona.security.owner_identity import OwnerIdentity

    monkeypatch.setenv("ANTIGONA_ENABLE_STARTUP_RECOVERY", "0")
    monkeypatch.setattr(OwnerIdentity, "is_owner", lambda self, user_id: True)
    bot = TelegramBot(
        token="123456789:ABCdefGHIjklMNOpqrsTUVwxyz",
        gateway_url="http://test-gateway:8090",
        gateway_token="test-token",
        database_url=f"sqlite:///{tmp_path / 'bot.db'}",
    )
    bot.bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=900))
    bot.operation_presenter._debounce = 0
    bot.event_bus.publish = AsyncMock()
    bot._store_session_message = AsyncMock()
    bot._update_last_response_time = MagicMock()
    bot._handle_plan_or_actions = AsyncMock(return_value=None)
    bot._operation_final_receipt = AsyncMock(return_value=None)
    bot._publish_operation_stage = AsyncMock()
    bot._publish_operation_final = AsyncMock(return_value=False)
    try:
        call = await _run_turn_with(bot, monkeypatch, "TASK_RESULT", "сделано", True)
    finally:
        await bot.close()
        await bot.gateway_client.close()
    state = call.kwargs.get("terminal_state")
    assert str(state) in ("SUCCEEDED", OperationState.SUCCEEDED.value), f"got {state}"


# ── Phase 5 / 3.2: explicit task-parameter contract (path reaches the backend) ──


@pytest.mark.asyncio
async def test_file_write_path_reaches_task_backend(tmp_path) -> None:
    """A file-write intent\'s extracted path must be passed to submit_task(path=...)."""
    from unittest.mock import AsyncMock, MagicMock

    from antigona.core.brain import AntigonaBrain
    from antigona.sessions.repository import SessionRepository

    repo = SessionRepository(db_path=str(tmp_path / "s.db"))
    engine = MagicMock()
    engine.reply = AsyncMock(return_value="<LLM>")
    b = AntigonaBrain(
        dialogue_engine=engine,
        session_repository=repo,
        db_path=str(tmp_path / "s.db"),
    )
    backend = MagicMock()
    backend.submit_task = AsyncMock(return_value={
        "flow_id": "flow-1",
        "requires_approval": False,
    })
    b.task_backend = backend
    try:
        await b.connect()
        await b.process(
            text="Create document txt xxxx.md tekst: ggbb",
            user_id="u1", channel="cli", session_id="cli:u1",
        )
    finally:
        await b.close()

    assert backend.submit_task.await_count == 1
    call_kwargs = backend.submit_task.call_args.kwargs
    assert call_kwargs.get("path") == "xxxx.md", f"path not propagated: {call_kwargs}"


# ── Phase 5: active-task follow-up continuity (steer/status, not smalltalk) ──


@pytest.fixture()
def active_ctx() -> dict:
    return {"source": "cli", "active_task_id": "flow-123"}


@pytest.mark.parametrize(
    "text",
    ["сохрани в docs", "сохрани в docs/notes.md", "положи в папку docs"],
)
def test_path_followup_steers_active_task(router: IntentRouter, active_ctx: dict, text: str) -> None:
    """A path-placement follow-up while a task is active must continue THAT task."""
    d = router.route(text=text, context=active_ctx)
    assert d.intent == "task.continue", f"got {d.intent}"
    assert d.entities.get("task_id") == "flow-123"
    assert d.entities.get("followup") == text


@pytest.mark.parametrize("text", ["создал?", "готово?", "готово ли"])
def test_status_followup_queries_active_task(router: IntentRouter, active_ctx: dict, text: str) -> None:
    """A status question while a task is active must query its real state."""
    d = router.route(text=text, context=active_ctx)
    assert d.intent == "command.status", f"got {d.intent}"
    assert d.entities.get("task_id") == "flow-123"


@pytest.mark.parametrize("text", ["привет", "сохрани в docs"])
def test_short_followup_without_active_task_is_not_forced(router: IntentRouter, text: str) -> None:
    """Without an active task the same short text must NOT be steered/status-queried."""
    d = router.route(text=text, context={"source": "cli"})
    assert d.intent != "task.continue"
    assert d.intent != "command.status"


# ── P1 TTS/AUDIO FALSE_DONE regression (2026-09-06) ─────────────────────────
# speech.tts success != user DONE. A TASK_RESULT whose reply carries a
# ⟪voice:path⟫ marker but whose audio file is MISSING (or send fails) must end
# as FAILED, never SUCCEEDED from the text alone.


@pytest.mark.asyncio
async def test_voice_marker_missing_file_is_not_fake_success(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    """A reply promising a voice artifact that does not exist must NOT be SUCCEEDED."""
    from antigona.security.owner_identity import OwnerIdentity

    monkeypatch.setenv("ANTIGONA_ENABLE_STARTUP_RECOVERY", "0")
    monkeypatch.setattr(OwnerIdentity, "is_owner", lambda self, user_id: True)
    bot = TelegramBot(
        token="123456789:ABCdefGHIjklMNOpqrsTUVwxyz",
        gateway_url="http://test-gateway:8090",
        gateway_token="test-token",
        database_url=f"sqlite:///{tmp_path / 'bot.db'}",
    )
    bot.bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=900))
    bot.operation_presenter._debounce = 0
    bot.event_bus.publish = AsyncMock()
    bot._store_session_message = AsyncMock()
    bot._update_last_response_time = MagicMock()
    bot._handle_plan_or_actions = AsyncMock(return_value=None)
    bot._operation_final_receipt = AsyncMock(return_value=None)
    bot._publish_operation_stage = AsyncMock()
    bot._publish_operation_final = AsyncMock(return_value=False)
    missing = str(tmp_path / "definitely_missing.tts.ogg")
    turn_mock = AsyncMock(return_value={
        "reply": f"Вот озвучка {chr(0x27ea)}voice:{missing}{chr(0x27eb)}",
        "session_id": "telegram:12345",
        "response_type": "TASK_RESULT",
        "flow_id": None,
        "requires_approval": False,
        "verified": True,
    })
    monkeypatch.setattr(bot.gateway_client, "send_dialogue_turn", turn_mock)
    try:
        await _text_handler(bot)(_make_bot_message("озвучь текст"))
        calls = [c for c in bot._publish_operation_final.call_args_list]
        assert calls, "final publish should be called"
        call = calls[-1]
        final_text = call.args[1] if len(call.args) > 1 else str(call.kwargs.get("text", ""))
        state = call.kwargs.get("terminal_state")
        assert str(state) in ("FAILED", OperationState.FAILED.value), (
            f"voice delivery failure must be FAILED, got {state}"
        )
        assert "✅ Готово" not in final_text, "voice delivery failure must not render success"
    finally:
        await bot.close()
        await bot.gateway_client.close()
