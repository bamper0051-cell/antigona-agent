"""Telegram Bridge v1 — production wiring, not helper-level behaviour.

Every probe here drives the *real* registered aiogram handler on a real
``TelegramBot`` (mocked Telegram + Gateway I/O only) so that the guarantees are
proven end to end: one runtime invocation per message, delivery failures that
never re-enter the runtime, ordered long-output delivery, artifact validation at
the single delivery owner, and chat-scoped ``/stop``.
"""
from __future__ import annotations

import asyncio
import gc
import warnings
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from antigona.channels.telegram.bot import TelegramBot
from antigona.channels.telegram.bridge import TELEGRAM_TEXT_LIMIT, utf16_length
from antigona.durable.operation_models import OperationState
from antigona.events.event_types import FinalResponseReady
from antigona.presentation.presenter import OperationPresenter
from antigona.security.owner_identity import OwnerIdentity

OWNER_ID = 4242


# ── Fixtures ─────────────────────────────────────────────────────────────────


def make_message(
    text: str = "сделай отчёт",
    *,
    chat_id: int = 900,
    message_id: int = 1,
    caption: str | None = None,
    document: Any = None,
    photo: Any = None,
    thread_id: int | None = None,
    edit_date: int | None = None,
) -> MagicMock:
    message = MagicMock()
    message.text = text
    message.caption = caption
    message.document = document
    message.photo = photo
    message.chat = MagicMock()
    message.chat.id = chat_id
    message.chat.type = "private"
    message.message_id = message_id
    message.message_thread_id = thread_id
    message.is_topic_message = thread_id is not None
    message.edit_date = edit_date
    message.from_user = MagicMock()
    message.from_user.id = OWNER_ID
    message.from_user.is_bot = False
    message.reply_to_message = None
    message.answer = AsyncMock(return_value=SimpleNamespace(message_id=555))
    message.bot = MagicMock()
    message.bot.send_chat_action = AsyncMock()
    return message


@pytest.fixture
async def bot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[TelegramBot]:
    monkeypatch.setenv("ANTIGONA_ENABLE_STARTUP_RECOVERY", "0")
    monkeypatch.setenv("ANTIGONA_TELEGRAM_TURN_LEDGER", str(tmp_path / "turns.db"))
    monkeypatch.setattr(
        TelegramBot,
        "_resolve_bot_info",
        AsyncMock(return_value=("antigona_test_bot", 424242)),
    )
    monkeypatch.setattr(OwnerIdentity, "is_configured", property(lambda self: True))
    monkeypatch.setattr(
        OwnerIdentity,
        "is_owner",
        lambda self, user_id: user_id == OWNER_ID,
    )

    instance = TelegramBot(
        token="123456789:ABCdefGHIjklMNOpqrsTUVwxyz",
        gateway_url="http://test-gateway:8090",
        gateway_token="test-token",
        database_url=f"sqlite:///{tmp_path / 'production.db'}",
    )

    receipt = 1000

    async def _send_message(**kwargs: Any) -> SimpleNamespace:
        nonlocal receipt
        receipt += 1
        return SimpleNamespace(message_id=receipt)

    instance.bot.send_message = AsyncMock(side_effect=_send_message)
    instance.bot.send_document = AsyncMock(
        side_effect=lambda **kw: SimpleNamespace(message_id=2001)
    )
    instance.bot.edit_message_text = AsyncMock()
    instance.bot.delete_message = AsyncMock()
    instance.operation_presenter._debounce = 0
    instance.operation_presenter._artifact_roots = ((tmp_path / "workspace").resolve(),)
    (tmp_path / "workspace").mkdir(exist_ok=True)
    await instance.startup()
    try:
        yield instance
    finally:
        await instance.close()


def handler(bot: TelegramBot, index: int = 0) -> Any:
    """Return the Nth registered message handler callback."""
    return bot.router.message.handlers[index].callback


def handler_named(bot: TelegramBot, name: str) -> Any:
    for observer in (bot.router.message, bot.router.edited_message):
        for registered in observer.handlers:
            if registered.callback.__name__ == name:
                return registered.callback
    raise AssertionError(f"handler {name} is not registered")


async def _wait_until(
    predicate: Any,
    *,
    timeout: float = 5.0,
) -> None:
    """Poll the event loop until ``predicate()`` holds, or fail loudly."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.001)
    raise AssertionError("condition was never reached")


def _pending_for(bot: TelegramBot, chat_id: int) -> int:
    session = bot.telegram_bridge._sessions.get(chat_id)
    return len(session.pending) if session is not None else 0


def _http_session_released(bot: TelegramBot) -> bool:
    """True when aiogram holds no *open* aiohttp ClientSession.

    aiogram creates the underlying session lazily, so "never opened" and
    "opened and closed" are both acceptable; "opened and still open" is the
    leak this asserts against.
    """
    underlying = getattr(bot.bot.session, "_session", None)
    return underlying is None or bool(underlying.closed)


def turn_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "reply": "Готово.",
        "session_id": "telegram:900",
        "response_type": "conversation",
        "flow_id": None,
        "requires_approval": False,
        "verified": None,
    }
    payload.update(overrides)
    return payload


# ── Exactly one runtime invocation ───────────────────────────────────────────


async def test_text_handler_invokes_runtime_exactly_once(bot: TelegramBot) -> None:
    gateway = AsyncMock(return_value=turn_payload())
    bot.gateway_client.send_dialogue_turn = gateway

    await handler_named(bot, "text_handler")(make_message(message_id=11))

    gateway.assert_awaited_once()
    assert gateway.await_args.kwargs["channel"] == "telegram"
    assert gateway.await_args.kwargs["session_id"] == "telegram:900"
    assert gateway.await_args.kwargs["turn_id"] == "telegram:900:-:message:11:0"


async def test_redelivered_update_does_not_rerun_the_runtime(
    bot: TelegramBot,
) -> None:
    gateway = AsyncMock(return_value=turn_payload())
    bot.gateway_client.send_dialogue_turn = gateway

    first = make_message(message_id=12)
    await handler_named(bot, "text_handler")(first)
    # Telegram redelivers the very same update.
    await handler_named(bot, "text_handler")(make_message(message_id=12))

    gateway.assert_awaited_once()


async def test_concurrent_duplicate_updates_invoke_runtime_once(
    bot: TelegramBot,
) -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def _turn(**kwargs: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return turn_payload()

    bot.gateway_client.send_dialogue_turn = AsyncMock(side_effect=_turn)
    text_handler = handler_named(bot, "text_handler")

    tasks = [
        asyncio.create_task(text_handler(make_message(message_id=13)))
        for _ in range(5)
    ]
    await started.wait()
    release.set()
    await asyncio.gather(*tasks)

    assert calls == 1


async def test_original_and_edit_both_reach_the_runtime(bot: TelegramBot) -> None:
    gateway = AsyncMock(return_value=turn_payload())
    bot.gateway_client.send_dialogue_turn = gateway

    await handler_named(bot, "text_handler")(make_message(message_id=20, text="v1"))
    await handler_named(bot, "edited_message_handler")(
        make_message(message_id=20, text="v2", edit_date=1_700_000_000)
    )
    # The same edit redelivered must not run a third time.
    await handler_named(bot, "edited_message_handler")(
        make_message(message_id=20, text="v2", edit_date=1_700_000_000)
    )

    assert gateway.await_count == 2
    turn_ids = [call.kwargs["turn_id"] for call in gateway.await_args_list]
    assert turn_ids == [
        "telegram:900:-:message:20:0",
        "telegram:900:-:edited:20:1700000000",
    ]


async def test_non_owner_never_reaches_the_runtime(bot: TelegramBot) -> None:
    gateway = AsyncMock(return_value=turn_payload())
    bot.gateway_client.send_dialogue_turn = gateway

    intruder = make_message(message_id=30)
    intruder.from_user.id = 9999
    await handler_named(bot, "text_handler")(intruder)

    gateway.assert_not_awaited()
    intruder.answer.assert_awaited()
    assert "Доступ запрещён" in intruder.answer.await_args.args[0]


# ── Delivery failure must never re-run the agent ─────────────────────────────


async def test_delivery_failure_never_reinvokes_the_runtime(
    bot: TelegramBot,
) -> None:
    gateway = AsyncMock(return_value=turn_payload(reply="дорогой результат"))
    bot.gateway_client.send_dialogue_turn = gateway
    bot.bot.send_message = AsyncMock(side_effect=RuntimeError("Telegram is down"))

    await handler_named(bot, "text_handler")(make_message(message_id=40))

    gateway.assert_awaited_once()


async def test_delivery_timeout_does_not_rerun_the_runtime(
    bot: TelegramBot,
) -> None:
    gateway = AsyncMock(return_value=turn_payload())
    bot.gateway_client.send_dialogue_turn = gateway
    bot.bot.send_message = AsyncMock(side_effect=TimeoutError("uncertain"))

    await handler_named(bot, "text_handler")(make_message(message_id=41))

    gateway.assert_awaited_once()


# ── Long output: ordered chunks, all receipts, one plain fallback ────────────


async def test_long_final_is_split_ordered_and_fully_receipted(
    bot: TelegramBot,
) -> None:
    await bot.dp.emit_startup()
    operation = await bot.operation_store.create(
        chat_id=900, user_id=OWNER_ID, text="long", message_id=50,
    )
    bot.bot.send_message.reset_mock()

    body = "".join(f"строка {n} 🙂\n" for n in range(1200))
    receipt = await bot.operation_presenter.deliver_final(
        FinalResponseReady(
            operation_id=operation.id,
            text=body,
            parse_mode="HTML",
            terminal_state=OperationState.SUCCEEDED.value,
        )
    )

    calls = bot.bot.send_message.await_args_list
    assert len(calls) > 1, "a >4096-unit answer must be split"
    assert receipt == calls[0].kwargs.get("message_id", 1001) or receipt > 0

    for call in calls:
        text = call.kwargs["text"]
        assert len(text.encode("utf-16-le")) // 2 <= 4096

    stored = await bot.operation_store.get(operation.id)
    assert stored is not None
    assert len(stored.final_message_ids) == len(calls), "every chunk needs a receipt"
    assert stored.final_message_ids == sorted(stored.final_message_ids)


async def test_entity_parse_failure_falls_back_to_plain_text_once(
    bot: TelegramBot,
) -> None:
    await bot.dp.emit_startup()
    operation = await bot.operation_store.create(
        chat_id=900, user_id=OWNER_ID, text="fmt", message_id=51,
    )

    attempts: list[str | None] = []

    async def _send(**kwargs: Any) -> SimpleNamespace:
        attempts.append(kwargs.get("parse_mode"))
        if kwargs.get("parse_mode") == "HTML":
            raise RuntimeError("Bad Request: can't parse entities: bad tag")
        return SimpleNamespace(message_id=1234)

    bot.bot.send_message = AsyncMock(side_effect=_send)

    receipt = await bot.operation_presenter.deliver_final(
        FinalResponseReady(
            operation_id=operation.id,
            text="<b>broken",
            parse_mode="HTML",
            terminal_state=OperationState.SUCCEEDED.value,
        )
    )

    assert receipt == 1234
    assert attempts == ["HTML", None], "exactly one downgrade, then no retry loop"


async def test_non_entity_send_error_does_not_downgrade(bot: TelegramBot) -> None:
    await bot.dp.emit_startup()
    operation = await bot.operation_store.create(
        chat_id=900, user_id=OWNER_ID, text="net", message_id=52,
    )
    attempts: list[str | None] = []

    async def _send(**kwargs: Any) -> SimpleNamespace:
        attempts.append(kwargs.get("parse_mode"))
        raise RuntimeError("Connection reset by peer")

    bot.bot.send_message = AsyncMock(side_effect=_send)
    receipt = await bot.operation_presenter.deliver_final(
        FinalResponseReady(
            operation_id=operation.id,
            text="hello",
            parse_mode="HTML",
            terminal_state=OperationState.SUCCEEDED.value,
        )
    )

    assert receipt is None
    assert attempts == ["HTML"], "a transport error is not a formatting error"


# ── Outbound artifacts through the single delivery owner ─────────────────────


async def test_allowed_artifact_is_delivered_after_the_final(
    bot: TelegramBot,
    tmp_path: Path,
) -> None:
    await bot.dp.emit_startup()
    artifact = tmp_path / "workspace" / "report.txt"
    artifact.write_text("results")
    operation = await bot.operation_store.create(
        chat_id=900, user_id=OWNER_ID, text="art", message_id=60,
    )

    await bot.operation_presenter.deliver_final(
        FinalResponseReady(
            operation_id=operation.id,
            text="готово",
            terminal_state=OperationState.SUCCEEDED.value,
            artifacts=(str(artifact),),
        )
    )

    bot.bot.send_document.assert_awaited_once()
    stored = await bot.operation_store.get(operation.id)
    assert stored is not None
    assert 2001 in stored.final_message_ids


@pytest.mark.parametrize("name", [".env", "id_rsa", "secrets.json"])
async def test_secret_artifact_is_refused_before_any_send(
    bot: TelegramBot,
    tmp_path: Path,
    name: str,
) -> None:
    await bot.dp.emit_startup()
    secret = tmp_path / "workspace" / name
    secret.write_text("TOKEN=abc")
    operation = await bot.operation_store.create(
        chat_id=900, user_id=OWNER_ID, text="art", message_id=61,
    )

    await bot.operation_presenter.deliver_final(
        FinalResponseReady(
            operation_id=operation.id,
            text="готово",
            terminal_state=OperationState.SUCCEEDED.value,
            artifacts=(str(secret),),
        )
    )

    bot.bot.send_document.assert_not_awaited()


async def test_artifact_outside_root_is_refused(
    bot: TelegramBot,
    tmp_path: Path,
) -> None:
    await bot.dp.emit_startup()
    outside = tmp_path / "elsewhere.txt"
    outside.write_text("nope")
    operation = await bot.operation_store.create(
        chat_id=900, user_id=OWNER_ID, text="art", message_id=62,
    )

    await bot.operation_presenter.deliver_final(
        FinalResponseReady(
            operation_id=operation.id,
            text="готово",
            terminal_state=OperationState.SUCCEEDED.value,
            artifacts=(str(outside),),
        )
    )

    bot.bot.send_document.assert_not_awaited()


async def test_core_supplied_artifacts_reach_the_presenter(
    bot: TelegramBot,
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "workspace" / "out.log"
    artifact.write_text("log line")
    bot.gateway_client.send_dialogue_turn = AsyncMock(
        return_value=turn_payload(artifacts=[str(artifact)])
    )
    await bot.dp.emit_startup()

    await handler_named(bot, "text_handler")(make_message(message_id=63))

    bot.bot.send_document.assert_awaited_once()


# ── Inbound attachments ──────────────────────────────────────────────────────


def _document(name: str = "notes.txt", size: int = 12) -> MagicMock:
    doc = MagicMock()
    doc.file_id = "AgAC-file-id"
    doc.file_name = name
    doc.file_size = size
    doc.mime_type = "text/plain"
    return doc


def _wire_download(message: MagicMock, payload: bytes = b"hello world!") -> None:
    message.bot.get_file = AsyncMock(
        return_value=SimpleNamespace(file_path="documents/file_1.txt")
    )

    async def _download(_path: str, destination: Any = None, **_kw: Any) -> None:
        destination.write(payload)

    message.bot.download_file = AsyncMock(side_effect=_download)


async def test_document_metadata_reaches_runtime_exactly_once(
    bot: TelegramBot,
) -> None:
    gateway = AsyncMock(return_value=turn_payload(reply="прочитал"))
    bot.gateway_client.send_dialogue_turn = gateway

    message = make_message(
        text=None,
        message_id=70,
        caption="разбери это",
        document=_document(),
    )
    _wire_download(message)

    await handler_named(bot, "document_handler")(message)

    gateway.assert_awaited_once()
    handoff = gateway.await_args.kwargs["text"]
    assert "[attachment:document]" in handoff
    assert "mime: text/plain" in handoff
    assert "size_bytes: 12" in handoff
    assert "telegram_file_id: AgAC-file-id" in handoff
    assert "handle: attachment://" in handoff
    assert "caption: разбери это" in handoff
    assert gateway.await_args.kwargs["turn_id"] == "telegram:900:-:document:70:0"
    message.answer.assert_awaited_once_with(
        "прочитал", reply_to_message_id=message.message_id
    )


async def test_document_long_reply_is_delivered_once_in_ordered_utf16_chunks(
    bot: TelegramBot,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reply = "".join(f"часть-{index} 🙂\n" for index in range(700))
    reply = reply.strip()
    assert utf16_length(reply) > TELEGRAM_TEXT_LIMIT
    bridge_turn = AsyncMock(return_value={"reply": reply})
    monkeypatch.setattr(bot, "_bridge_turn", bridge_turn)

    message = make_message(text="", message_id=701, document=_document())
    _wire_download(message)

    await handler_named(bot, "document_handler")(message)

    bridge_turn.assert_awaited_once()
    calls = message.answer.await_args_list
    assert len(calls) > 1
    assert "".join(call.args[0] for call in calls) == reply
    for call in calls:
        assert utf16_length(call.args[0]) <= TELEGRAM_TEXT_LIMIT
        assert call.kwargs == {
            "reply_to_message_id": message.message_id,
            "parse_mode": None,
        }


async def test_photo_uses_the_same_secure_path(bot: TelegramBot) -> None:
    gateway = AsyncMock(return_value=turn_payload(reply="вижу"))
    bot.gateway_client.send_dialogue_turn = gateway

    photo = SimpleNamespace(file_id="photo-id", file_size=9)
    message = make_message(
        text=None,
        message_id=71,
        caption="что тут?",
        photo=[SimpleNamespace(file_id="small", file_size=2), photo],
    )
    _wire_download(message, payload=b"jpegbytes")

    await handler_named(bot, "photo_handler")(message)

    gateway.assert_awaited_once()
    handoff = gateway.await_args.kwargs["text"]
    assert "[attachment:photo]" in handoff
    assert "mime: image/jpeg" in handoff
    assert "telegram_file_id: photo-id" in handoff, "largest size must be chosen"


async def test_hostile_document_name_is_confined(bot: TelegramBot) -> None:
    gateway = AsyncMock(return_value=turn_payload())
    bot.gateway_client.send_dialogue_turn = gateway

    message = make_message(
        text=None,
        message_id=72,
        document=_document(name="../../../../etc/passwd"),
    )
    _wire_download(message)

    await handler_named(bot, "document_handler")(message)

    handoff = gateway.await_args.kwargs["text"]
    assert "/etc/passwd" not in handoff
    stored = [line for line in handoff.splitlines() if line.startswith("path: ")][0]
    assert "/downloads/900/" in stored.replace("\\", "/")


async def test_oversized_document_is_rejected_before_download(
    bot: TelegramBot,
) -> None:
    gateway = AsyncMock(return_value=turn_payload())
    bot.gateway_client.send_dialogue_turn = gateway

    message = make_message(
        text=None,
        message_id=73,
        document=_document(size=999 * 1024 * 1024),
    )
    _wire_download(message)

    await handler_named(bot, "document_handler")(message)

    gateway.assert_not_awaited()
    message.bot.download_file.assert_not_awaited()
    assert "политикой безопасности" in message.answer.await_args.args[0]


async def test_partial_download_is_cleaned_up_on_failure(
    bot: TelegramBot,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "antigona.core.paths.downloads_dir",
        lambda: tmp_path / "downloads",
    )
    gateway = AsyncMock(return_value=turn_payload())
    bot.gateway_client.send_dialogue_turn = gateway

    message = make_message(text=None, message_id=74, document=_document())
    message.bot.get_file = AsyncMock(
        return_value=SimpleNamespace(file_path="documents/f.txt")
    )

    async def _explode(_path: str, destination: Any = None, **_kw: Any) -> None:
        destination.write(b"partial")
        raise RuntimeError("network died mid-download")

    message.bot.download_file = AsyncMock(side_effect=_explode)

    await handler_named(bot, "document_handler")(message)

    gateway.assert_not_awaited()
    leftovers = list((tmp_path / "downloads").rglob("*.part"))
    assert leftovers == [], "a partial download must never be left behind"


async def test_non_owner_cannot_upload(bot: TelegramBot) -> None:
    gateway = AsyncMock(return_value=turn_payload())
    bot.gateway_client.send_dialogue_turn = gateway

    message = make_message(text=None, message_id=75, document=_document())
    message.from_user.id = 9999
    _wire_download(message)

    await handler_named(bot, "document_handler")(message)

    gateway.assert_not_awaited()
    message.bot.get_file.assert_not_awaited()


# ── /stop ────────────────────────────────────────────────────────────────────


async def test_stop_without_active_work_is_benign_and_idempotent(
    bot: TelegramBot,
) -> None:
    bot.gateway_client.cancel = AsyncMock()
    message = make_message(text="/stop", message_id=80)

    await handler_named(bot, "stop_handler")(message)
    await handler_named(bot, "stop_handler")(message)

    bot.gateway_client.cancel.assert_not_awaited()
    assert "Нет активной работы" in message.answer.await_args.args[0]


async def test_stop_cancels_the_active_flow_for_this_chat(
    bot: TelegramBot,
) -> None:
    bot.gateway_client.cancel = AsyncMock()
    operation = await bot.operation_store.create(
        chat_id=900, user_id=OWNER_ID, text="долгая задача", message_id=81,
    )
    await bot.operation_store.set_flow_id(operation.id, "flow-abc-123")

    message = make_message(text="/stop", message_id=82)
    await handler_named(bot, "stop_handler")(message)

    bot.gateway_client.cancel.assert_awaited_once()
    assert bot.gateway_client.cancel.await_args.kwargs["flow_id"] == "flow-abc-123"


async def test_stop_in_one_chat_never_touches_another(bot: TelegramBot) -> None:
    bot.gateway_client.cancel = AsyncMock()
    other = await bot.operation_store.create(
        chat_id=901, user_id=OWNER_ID, text="чужая задача", message_id=83,
    )
    await bot.operation_store.set_flow_id(other.id, "flow-other")

    await handler_named(bot, "stop_handler")(make_message(text="/stop", chat_id=900))

    bot.gateway_client.cancel.assert_not_awaited()


async def test_stop_is_owner_gated(bot: TelegramBot) -> None:
    bot.gateway_client.cancel = AsyncMock()
    message = make_message(text="/stop", message_id=84)
    message.from_user.id = 9999

    await handler_named(bot, "stop_handler")(message)

    bot.gateway_client.cancel.assert_not_awaited()
    assert "Доступ запрещён" in message.answer.await_args.args[0]


async def test_stop_drops_queued_turns_for_this_chat(bot: TelegramBot) -> None:
    release = asyncio.Event()

    async def _turn(**kwargs: Any) -> dict[str, Any]:
        await release.wait()
        return turn_payload()

    bot.gateway_client.send_dialogue_turn = AsyncMock(side_effect=_turn)
    bot.gateway_client.cancel = AsyncMock()
    text_handler = handler_named(bot, "text_handler")

    running = asyncio.create_task(text_handler(make_message(message_id=90)))
    await _wait_until(lambda: bot.gateway_client.send_dialogue_turn.await_count == 1)

    queued = asyncio.create_task(text_handler(make_message(message_id=91)))
    # Operation creation does real SQLite I/O, so wait for the turn to actually
    # reach the bridge queue rather than guessing at a tick count.
    await _wait_until(lambda: _pending_for(bot, 900) == 1)

    await handler_named(bot, "stop_handler")(make_message(text="/stop", message_id=92))
    await queued  # the handler reports cancellation instead of hanging

    release.set()
    await running
    assert bot.gateway_client.send_dialogue_turn.await_count == 1


# ── Approval command regressions ─────────────────────────────────────────────


@pytest.mark.parametrize("command", ["/approve", "/deny"])
async def test_approval_commands_still_registered(
    bot: TelegramBot,
    command: str,
) -> None:
    name = "approve_handler" if command == "/approve" else "deny_handler"
    assert handler_named(bot, name) is not None


# ── Resource lifecycle ───────────────────────────────────────────────────────


async def test_full_cycle_leaves_no_unclosed_resources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Warning-sensitive probe: a whole bot lifecycle must leak nothing."""
    monkeypatch.setenv("ANTIGONA_ENABLE_STARTUP_RECOVERY", "0")
    monkeypatch.setenv("ANTIGONA_TELEGRAM_TURN_LEDGER", str(tmp_path / "t.db"))
    monkeypatch.setattr(
        TelegramBot,
        "_resolve_bot_info",
        AsyncMock(return_value=("antigona_test_bot", 1)),
    )
    monkeypatch.setattr(OwnerIdentity, "is_configured", property(lambda self: True))
    monkeypatch.setattr(OwnerIdentity, "is_owner", lambda self, u: u == OWNER_ID)

    # Flush garbage left by earlier, unrelated tests before opening this probe.
    # The catch scope must attribute warnings only to this bot lifecycle.
    gc.collect()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")

        instance = TelegramBot(
            token="123456789:ABCdefGHIjklMNOpqrsTUVwxyz",
            gateway_url="http://test-gateway:8090",
            gateway_token="test-token",
            database_url=f"sqlite:///{tmp_path / 'leak.db'}",
        )
        instance.bot.send_message = AsyncMock(
            return_value=SimpleNamespace(message_id=1)
        )
        instance.bot.edit_message_text = AsyncMock()
        instance.bot.delete_message = AsyncMock()
        instance.gateway_client.send_dialogue_turn = AsyncMock(
            return_value=turn_payload()
        )
        await handler_named(instance, "text_handler")(make_message(message_id=99))
        await instance.close()

        # The aiogram session must be released, not merely dereferenced.
        assert _http_session_released(instance)
        # No bridge worker may survive close().
        assert instance.telegram_bridge.session_count == 0

        del instance
        gc.collect()
        await asyncio.sleep(0)

    leaks = [
        str(w.message)
        for w in caught
        if "unclosed" in str(w.message).lower()
        or "coroutine" in str(w.message).lower()
    ]
    assert leaks == [], f"resource leak detected: {leaks}"


async def test_close_is_idempotent(bot: TelegramBot) -> None:
    await bot.close()
    await bot.close()
    assert _http_session_released(bot)


async def test_presenter_artifact_roots_default_to_safe_locations() -> None:
    presenter = OperationPresenter(MagicMock(), MagicMock(), MagicMock())
    assert presenter._artifact_roots
    assert all(root.is_absolute() for root in presenter._artifact_roots)
