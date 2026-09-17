"""Turn truthfulness & observability regressions (2026-09-10 wave).

Covers the live-exam findings:
  D-2  a denied/failed tool turn must present a FAILURE and persist a
       non-SUCCEEDED operation with a real last_error (never "✅ Готово"/SUCCEEDED).
  D-2b a multi-step PARTIAL outcome is a non-success, distinct from full success.
  D-1  the REAL Telegram outbound message id is persisted and the inbound<->
       outbound correlation is recorded (BindingRepository actually commits).
  D-3  an attachment/transport-only turn creates an operation record.
  D-4  internal security wording (fencing token / ownership internals) never
       leaks into the user-visible chat text.
"""
from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from antigona.channels.telegram.bot import (
    TelegramBot,
    _sanitize_user_facing_error,
)
from antigona.durable.operation_models import OperationState


def _text_handler(bot: TelegramBot):
    return next(
        h.callback
        for h in bot.router.message.handlers
        if h.callback.__name__ == "text_handler"
    )


def _make_bot_message(text: str, message_id: int = 1) -> MagicMock:
    msg = MagicMock()
    msg.text = text
    msg.chat = MagicMock(); msg.chat.id = 12345; msg.chat.type = "private"
    msg.message_id = message_id
    msg.from_user = MagicMock(); msg.from_user.id = 99999
    msg.from_user.is_bot = False
    msg.reply_to_message = None
    msg.bot = AsyncMock(); msg.bot.send_chat_action = AsyncMock()
    msg.answer = AsyncMock(); msg.answer.__name__ = "answer"
    msg.html_text = text
    return msg


def _make_bot(monkeypatch, tmp_path) -> TelegramBot:
    from antigona.security.owner_identity import OwnerIdentity

    monkeypatch.setenv("ANTIGONA_ENABLE_STARTUP_RECOVERY", "0")
    monkeypatch.setattr(OwnerIdentity, "is_owner", lambda self, user_id: True)
    bot = TelegramBot(
        token="123456789:ABCdefGHIjklMNOpqrsTUVwxyz",
        gateway_url="http://test-gateway:8090",
        gateway_token="test-token",
        database_url=f"sqlite:///{tmp_path / 'bot.db'}",
    )
    # The operations table must exist so the turn can durably record its
    # outcome (the runtime does this in startup()).
    bot.database.create_all()
    bot.bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=900))
    bot.operation_presenter._debounce = 0
    bot.event_bus.publish = AsyncMock()
    bot._store_session_message = AsyncMock()
    bot._update_last_response_time = MagicMock()
    bot._handle_plan_or_actions = AsyncMock(return_value=None)
    bot._operation_final_receipt = AsyncMock(return_value=None)
    bot._publish_operation_stage = AsyncMock()
    bot._publish_operation_final = AsyncMock(return_value=False)
    return bot


async def _run_turn(bot, monkeypatch, response_type, reply, *, verified=None,
                    tool_outcome=None, last_error=None):
    turn_mock = AsyncMock(return_value={
        "reply": reply,
        "session_id": "telegram:12345",
        "response_type": response_type,
        "flow_id": None,
        "requires_approval": False,
        "verified": verified,
        "tool_outcome": tool_outcome,
        "last_error": last_error,
    })
    monkeypatch.setattr(bot.gateway_client, "send_dialogue_turn", turn_mock)
    await _text_handler(bot)(_make_bot_message("выполни действие"))
    calls = [c for c in bot._publish_operation_final.call_args_list]
    assert calls, "final publish should be called"
    return calls[-1]


# ── D-2: denied / failed tool is never a success ─────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["FAILED", "DENIED"])
async def test_denied_tool_is_failure_with_last_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path, outcome,
) -> None:
    bot = _make_bot(monkeypatch, tmp_path)
    spy = AsyncMock(wraps=bot.operation_store.set_last_error)
    monkeypatch.setattr(bot.operation_store, "set_last_error", spy)
    try:
        call = await _run_turn(
            bot, monkeypatch, "CONVERSATION",
            "Ошибка выполнения: ownership enabled but write surface "
            "'workspace.write_text' has no fencing token; denying before any "
            "mutation (fail-closed)",
            tool_outcome=outcome,
            last_error=(
                "ownership enabled but write surface 'workspace.write_text' has "
                "no fencing token; denying before any mutation (fail-closed)"
            ),
        )
    finally:
        await bot.close()
        await bot.gateway_client.close()

    state = call.kwargs.get("terminal_state")
    assert str(state) in ("FAILED", OperationState.FAILED.value), f"got {state}"
    text = call.args[1] if len(call.args) > 1 else call.kwargs.get("text", "")
    assert "✅ Готово" not in text
    # A real last_error is persisted on the operation.
    assert spy.await_count >= 1, "operation.last_error must be written"
    persisted = spy.await_args.args[1]
    assert persisted and persisted.strip(), "last_error must be non-empty"


@pytest.mark.asyncio
async def test_partial_multistep_is_not_full_success(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    bot = _make_bot(monkeypatch, tmp_path)
    try:
        call = await _run_turn(
            bot, monkeypatch, "CONVERSATION",
            "Создал 2 из 3 файлов.",
            tool_outcome="PARTIAL", last_error="step 3 failed",
        )
    finally:
        await bot.close()
        await bot.gateway_client.close()
    state = call.kwargs.get("terminal_state")
    assert str(state) in ("FAILED", OperationState.FAILED.value), f"got {state}"
    text = call.args[1] if len(call.args) > 1 else call.kwargs.get("text", "")
    assert "частично" in text.lower(), text


@pytest.mark.asyncio
async def test_successful_conversation_still_succeeds(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    bot = _make_bot(monkeypatch, tmp_path)
    try:
        call = await _run_turn(
            bot, monkeypatch, "CONVERSATION", "Всё готово.",
            tool_outcome="SUCCEEDED",
        )
    finally:
        await bot.close()
        await bot.gateway_client.close()
    state = call.kwargs.get("terminal_state")
    assert str(state) in ("SUCCEEDED", OperationState.SUCCEEDED.value), f"got {state}"


# ── D-4: no internal security jargon in the user-visible text ────────────────


def test_sanitizer_removes_fencing_token_wording() -> None:
    raw = (
        "Ошибка выполнения: ownership enabled but write surface "
        "'workspace.write_text' has no fencing token; denying before any "
        "mutation (fail-closed)"
    )
    safe = _sanitize_user_facing_error(raw)
    low = safe.lower()
    for marker in ("fencing token", "ownership", "fail-closed", "write surface"):
        assert marker not in low, f"{marker!r} leaked: {safe!r}"
    assert "отклон" in low or "границ" in low, safe  # honest + actionable


@pytest.mark.asyncio
async def test_denied_turn_text_has_no_internal_jargon(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    bot = _make_bot(monkeypatch, tmp_path)
    try:
        call = await _run_turn(
            bot, monkeypatch, "CONVERSATION",
            "Ошибка выполнения: no fencing token; ownership fail-closed",
            tool_outcome="DENIED", last_error="no fencing token",
        )
    finally:
        await bot.close()
        await bot.gateway_client.close()
    text = (call.args[1] if len(call.args) > 1 else call.kwargs.get("text", "")).lower()
    assert "fencing token" not in text
    assert "ownership" not in text


# ── D-1: real outbound id persisted + inbound<->outbound correlation ─────────


@pytest.mark.asyncio
async def test_binding_repository_commits_and_correlates(tmp_path) -> None:
    from antigona.database import Database
    from antigona.input_pipeline.binding_repository import BindingRepository

    db = Database(f"sqlite:///{tmp_path / 'b.db'}")
    db.create_all()
    repo = BindingRepository(db)
    await repo.save(
        chat_id=12345,
        telegram_message_id=7813,
        user_id=None,
        task_id="flow-1",
        correlation_id="corr-1",
        message_role="assistant",
        message_kind="task_result",
        source_message_id=7811,
    )
    # A *fresh* session must see the row: proves the write was committed, not
    # merely flushed inside a session that rolls back on exit (the D-1 gap).
    other = BindingRepository(Database(f"sqlite:///{tmp_path / 'b.db'}"))
    row = await other.get_by_message_id(12345, 7813)
    assert row is not None, "binding was not committed"
    assert row.telegram_message_id == 7813
    assert row.source_message_id == 7811, "inbound<->outbound correlation missing"
    assert row.message_role == "assistant"


# ── D-3: attachment / transport-only turn creates an operation record ────────


@pytest.mark.asyncio
async def test_attachment_turn_creates_operation(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    from pathlib import Path

    import antigona.channels.telegram.bot as bot_mod

    bot = _make_bot(monkeypatch, tmp_path)
    monkeypatch.setattr(
        bot_mod, "should_process_message", lambda *a, **k: True
    )
    monkeypatch.setattr(
        bot, "_resolve_bot_info", AsyncMock(return_value=(None, None))
    )
    monkeypatch.setattr(
        bot_mod.paths, "downloads_dir", lambda: tmp_path
    )

    fd = os.open(str(tmp_path / "slot.bin"), os.O_RDWR | os.O_CREAT)
    slot = SimpleNamespace(path=Path(str(tmp_path / "slot.bin")), handle=fd)
    monkeypatch.setattr(bot_mod, "prepare_inbound_file", lambda *a, **k: slot)
    monkeypatch.setattr(bot_mod, "finalize_inbound_file", lambda *a, **k: 3)
    monkeypatch.setattr(bot_mod, "discard_inbound_file", lambda *a, **k: None)

    bot._bridge_turn = AsyncMock(return_value={
        "reply": "📎 Файл принят.", "flow_id": None, "tool_outcome": "SUCCEEDED",
    })
    create_spy = AsyncMock(wraps=bot.operation_store.create)
    monkeypatch.setattr(bot.operation_store, "create", create_spy)

    msg = MagicMock()
    msg.chat = MagicMock(); msg.chat.id = 12345
    msg.message_id = 7815
    msg.from_user = MagicMock(); msg.from_user.id = 99999
    msg.caption = None
    msg.reply_to_message = None
    msg.document = SimpleNamespace(
        file_id="F1", file_name="a.txt", file_size=3, mime_type="text/plain"
    )
    msg.bot = AsyncMock()
    msg.bot.get_file = AsyncMock(return_value=SimpleNamespace(file_path="p"))
    msg.bot.download_file = AsyncMock()
    msg.bot.send_chat_action = AsyncMock()
    msg.answer = AsyncMock()

    try:
        await bot._handle_inbound_attachment(msg, kind="document")
        assert create_spy.await_count == 1, "attachment turn must create an operation"
        op_id = create_spy.await_args.kwargs.get("message_id")
        assert op_id == 7815
    finally:
        os.close(fd)
        await bot.close()
        await bot.gateway_client.close()


# ── Truth contract: conversation-turn tool outcome propagates to the client ──


@pytest.mark.asyncio
async def test_brain_conversation_propagates_tool_outcome(tmp_path) -> None:
    from antigona.core.brain import AntigonaBrain
    from antigona.sessions.repository import SessionRepository

    engine = MagicMock()
    engine.reply = AsyncMock(return_value="Инструмент `write_file` не выполнен")
    engine._last_tool_outcome = "DENIED"
    engine._last_tool_error = "ownership fence: no fencing token"
    repo = SessionRepository(db_path=str(tmp_path / "s.db"))
    brain = AntigonaBrain(
        dialogue_engine=engine,
        session_repository=repo,
        db_path=str(tmp_path / "s.db"),
    )
    resp = await brain._handle_conversation("запиши файл", "telegram:1", None)
    assert resp.metadata.get("tool_outcome") == "DENIED"
    assert resp.metadata.get("last_error")


# ── Truthfulness: a no-tool conversation turn never replays a stale failure ──


@pytest.mark.asyncio
async def test_conversation_no_tool_turn_never_replays_stale_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    """A plain conversation turn (tool_outcome=None) must not present a
    previous turn's fail-closed error as this turn's result."""
    bot = _make_bot(monkeypatch, tmp_path)
    try:
        call = await _run_turn(
            bot, monkeypatch, "CONVERSATION",
            "Ошибка выполнения: ownership enabled but write surface "
            "'sandbox.shell' has no fencing token; denying before any mutation (fail-closed)",
            tool_outcome=None,
            last_error=None,
        )
    finally:
        await bot.close()
        await bot.gateway_client.close()

    text = (call.args[1] if len(call.args) > 1 else call.kwargs.get("text", "")).lower()
    assert "fencing token" not in text, text
    assert "ownership" not in text, text
    assert "fail-closed" not in text, text
    assert "ничего не выполняла" in text, text
    state = call.kwargs.get("terminal_state")
    assert str(state) in ("SUCCEEDED", OperationState.SUCCEEDED.value), f"got {state}"


@pytest.mark.asyncio
async def test_tool_outcome_none_conversation_with_normal_reply_untouched(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    bot = _make_bot(monkeypatch, tmp_path)
    try:
        call = await _run_turn(
            bot, monkeypatch, "CONVERSATION", "Привет! Чем помочь?",
            tool_outcome=None,
        )
    finally:
        await bot.close()
        await bot.gateway_client.close()
    text = call.args[1] if len(call.args) > 1 else call.kwargs.get("text", "")
    assert "Привет! Чем помочь?" in text
