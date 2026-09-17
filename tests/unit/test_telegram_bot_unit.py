from __future__ import annotations

from pathlib import Path

from antigona.channels.telegram.bot import (
    ApprovalCallback,
    TelegramBot,
    acquire_pid_lock,
    chitchat_reply,
    format_card,
    is_chitchat_or_noise,
    make_approval_keyboard,
    resolve_approval_token,
    should_process_message,
)


def test_telegram_bot_construction() -> None:
    bot_app = TelegramBot(
        token="123456789:ABCdefGHIjklMNOpqrsTUVwxyz",
        gateway_url="http://localhost:8090",
        gateway_token="dev-token",
    )
    assert bot_app.token == "123456789:ABCdefGHIjklMNOpqrsTUVwxyz"
    assert bot_app.gateway_url == "http://localhost:8090"
    assert bot_app.gateway_token == "dev-token"
    assert bot_app.dp is not None


def test_approval_callback_data_length() -> None:
    # Telegram callback_data limit is 64 bytes
    cb_approve = ApprovalCallback(
        approval_id="12345678-1234-1234-1234-123456789abc", action="approve"
    )
    packed_approve = cb_approve.pack()
    assert len(packed_approve.encode("utf-8")) <= 64

    cb_reject = ApprovalCallback(
        approval_id="12345678-1234-1234-1234-123456789abc", action="reject"
    )
    packed_reject = cb_reject.pack()
    assert len(packed_reject.encode("utf-8")) <= 64


def test_chitchat_single_letter_is_noise() -> None:
    assert is_chitchat_or_noise("S") is True
    assert is_chitchat_or_noise("Ы") is True
    assert is_chitchat_or_noise("?") is True


def test_chitchat_greetings_are_noise() -> None:
    # is_chitchat_or_noise is a pure length heuristic: only <=3-char inputs
    # are treated as noise.  Longer greetings are real messages (routed by
    # the IntentRouter as conversation), not length-noise.
    assert is_chitchat_or_noise("hi") is True
    assert is_chitchat_or_noise("хай") is True  # 3 chars
    for greeting in ["привет", "Привет!", "hello", "ping", "хелло"]:
        assert is_chitchat_or_noise(greeting) is False, greeting


def test_chitchat_thanks_are_noise() -> None:
    # Only short thanks count as length-noise; longer ones are real messages.
    assert is_chitchat_or_noise("ок") is True
    assert is_chitchat_or_noise("ok") is True
    assert is_chitchat_or_noise("спс") is True  # 3 chars
    for word in ["спасибо", "пасиб", "thanks"]:
        assert is_chitchat_or_noise(word) is False, word


def test_chitchat_punctuation_only_is_noise() -> None:
    for text in ["?", "!", "??", "..."]:
        assert is_chitchat_or_noise(text) is True, text


def test_chitchat_short_noise_words() -> None:
    assert is_chitchat_or_noise("ну") is True
    # 'да ладно' is >3 chars — now a real message, not length-noise.
    assert is_chitchat_or_noise("да ладно") is False


def test_real_task_with_action_verb_is_not_noise() -> None:
    assert is_chitchat_or_noise("создай файл hello.txt") is False
    assert is_chitchat_or_noise("напиши отчет по проекту") is False
    assert is_chitchat_or_noise("create file hello.txt with content Hello World") is False
    assert is_chitchat_or_noise("fix the bug in the parser module") is False


def test_shell_prefix_is_always_task() -> None:
    assert is_chitchat_or_noise("shell: ls") is False
    assert is_chitchat_or_noise("shell:") is False


def test_longer_message_without_action_verb_is_still_task() -> None:
    # Longer content-bearing message without a recognized verb but with
    # enough words should not be treated as pure noise.
    text = "нужно чтобы к завтрашнему дню отчет по продажам был готов"
    assert is_chitchat_or_noise(text) is False


def test_empty_and_whitespace_are_noise() -> None:
    assert is_chitchat_or_noise("") is True
    assert is_chitchat_or_noise("   ") is True


def test_chitchat_reply_content() -> None:
    # chitchat_reply is LLM-based — it returns whatever the provider produces,
    # never canned text like "Antigona"/"Пожалуйста".  Assert the provider path.
    from antigona.providers.mock import MockProvider

    reply = chitchat_reply("привет", provider=MockProvider(responses=["Привет, Владелец! 👋"]))
    assert isinstance(reply, str)
    assert len(reply) > 0
    # Distinct inputs still route through the provider (LLM-routed, not canned).
    assert isinstance(chitchat_reply("S", provider=MockProvider(responses=["ok"])), str)


def test_format_card() -> None:
    flow_data = {
        "id": "flow-123",
        "goal": "Test Goal",
        "status": "RUNNING",
        "target_path": "test.txt",
        "steps": [{"title": "Step 1", "status": "COMPLETED"}],
    }
    card = format_card(flow_data)
    assert "flow-123" in card
    assert "Test Goal" in card
    assert "RUNNING" in card


def test_make_approval_keyboard() -> None:
    flow_data = {
        "id": "flow-123",
        "approvals": [
            {
                "id": "appr-789",
                "risk_level": "HIGH",
                "reason": "Shell tool execution",
                "decision": "PENDING",
            }
        ],
    }
    kb = make_approval_keyboard(flow_data)
    assert kb is not None
    assert len(kb.inline_keyboard) == 1
    buttons = kb.inline_keyboard[0]
    assert len(buttons) == 2
    # Callback packs a compact token; unpack + registry resolve the original ids.
    approve = ApprovalCallback.unpack(buttons[0].callback_data)
    assert approve.action == "approve"
    assert resolve_approval_token(approve.approval_id) == ("appr-789", "flow-123")
    reject = ApprovalCallback.unpack(buttons[1].callback_data)
    assert reject.action == "reject"


def test_acquire_pid_lock(tmp_path: Path) -> None:
    pid_path = str(tmp_path / "test_bot.pid")
    lock1 = acquire_pid_lock(pid_path)
    assert lock1 is not None

    # Second acquire should fail while lock1 is held
    lock2 = acquire_pid_lock(pid_path)
    assert lock2 is None

    lock1.close()


class MockUser:
    def __init__(self, id_val: int = 123, is_bot: bool = False):
        self.id = id_val
        self.is_bot = is_bot


class MockChat:
    def __init__(self, type_val: str = "private"):
        self.type = type_val


class MockEntity:
    def __init__(self, type_val: str, offset: int = 0, length: int = 0, user: MockUser | None = None):
        self.type = type_val
        self.offset = offset
        self.length = length
        self.user = user


class MockMessage:
    def __init__(
        self,
        text: str | None = None,
        caption: str | None = None,
        chat_type: str = "private",
        from_user: MockUser | None = None,
        reply_to_message: MockMessage | None = None,
        entities: list[MockEntity] | None = None,
        caption_entities: list[MockEntity] | None = None,
    ):
        self.text = text
        self.caption = caption
        self.chat = MockChat(chat_type)
        self.from_user = from_user or MockUser()
        self.reply_to_message = reply_to_message
        self.entities = entities or []
        self.caption_entities = caption_entities or []


def test_telegram_bot_addressing_gates() -> None:
    bot_username = "antigona_bot"
    bot_id = 99999

    # 1. private + обычный текст -> process
    msg1 = MockMessage(text="Привет!", chat_type="private")
    assert should_process_message(msg1, bot_username, bot_id) is True

    # 2. private + сообщение от бота -> ignore
    msg2 = MockMessage(text="Привет!", chat_type="private", from_user=MockUser(is_bot=True))
    assert should_process_message(msg2, bot_username, bot_id) is False

    # 3. group + текст без адресации -> ignore
    msg3 = MockMessage(text="Просто сообщение", chat_type="group")
    assert should_process_message(msg3, bot_username, bot_id) is False

    # 4. group + команда "/start" -> process
    msg4 = MockMessage(text="/start", chat_type="group")
    assert should_process_message(msg4, bot_username, bot_id) is True

    # 5. group + реплай на сообщение бота -> process
    reply_to_bot = MockMessage(text="Hello", from_user=MockUser(is_bot=True))
    msg5 = MockMessage(text="Да, согласен", chat_type="group", reply_to_message=reply_to_bot)
    assert should_process_message(msg5, bot_username, bot_id) is True

    # 6. group + "@username_бота" в тексте (entity mention) -> process
    # "@antigona_bot" starts at offset 0 with length 13
    mention_entity = MockEntity(type_val="mention", offset=0, length=13)
    msg6 = MockMessage(text="@antigona_bot сделай отчет", chat_type="group", entities=[mention_entity])
    assert should_process_message(msg6, bot_username, bot_id) is True

    # 7. group + реплай на чужое сообщение без упоминания -> ignore
    reply_to_human = MockMessage(text="Hello", from_user=MockUser(is_bot=False))
    msg7 = MockMessage(text="Просто ответ", chat_type="group", reply_to_message=reply_to_human)
    assert should_process_message(msg7, bot_username, bot_id) is False

    # 8. group + text_mention with bot_id -> process
    text_mention_entity = MockEntity(type_val="text_mention", user=MockUser(id_val=bot_id))
    msg8 = MockMessage(text="Эй, бот", chat_type="group", entities=[text_mention_entity])
    assert should_process_message(msg8, bot_username, bot_id) is True


