"""Unit tests for DialogueEngine (Stage 1).

Герметичные и детерминированные: реальный LLM (DeepSeek и т.п.) в unit-тестах
НЕ вызывается. Все тесты внедряют stub-провайдер, который либо падает (→
детерминированный fallback), либо возвращает фиксированный ответ. Живой LLM
проверяется отдельным smoke-тестом на реальном стеке.
"""

from __future__ import annotations

import tempfile
from typing import Any
from unittest.mock import MagicMock

import pytest

from antigona.cli_ui.chat import ChatController
from antigona.conversation.dialogue_engine import DialogueEngine, clean_telegram_tags
from antigona.providers.base import BaseProvider, ProviderError
from antigona.sessions.repository import SessionRepository


class _FailingProvider(BaseProvider):
    """Stub: всегда бросает ProviderError → engine использует fallback."""

    name = "stub-failing"

    def generate(
        self,
        messages: list[dict[str, str]],
        context: dict[str, Any] | None = None,
    ) -> str:
        raise ProviderError("stub provider always fails (hermetic test)")


class _FixedProvider(BaseProvider):
    """Stub: возвращает фиксированный ответ (для тестов provider-path)."""

    name = "stub-fixed"

    def __init__(self, reply: str = "Фиксированный ответ.") -> None:
        self.reply = reply

    def generate(
        self,
        messages: list[dict[str, str]],
        context: dict[str, Any] | None = None,
    ) -> str:
        return self.reply


def test_clean_telegram_tags() -> None:
    raw_prompt = (
        "Ты — Antigona.\n"
        "Если пользователь просит создать файл — ответь командой:\n"
        "WRITE_FILE|путь|содержимое\n"
        "RUN_SHELL|ls -la\n"
        "Отвечай кратко."
    )
    cleaned = clean_telegram_tags(raw_prompt)
    assert "WRITE_FILE|" not in cleaned
    assert "RUN_SHELL|" not in cleaned
    assert "Ты — Antigona." in cleaned
    assert "Отвечай кратко." in cleaned


@pytest.mark.asyncio
async def test_dialogue_engine_context_builder_injection() -> None:
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as tmp_db:
        async with DialogueEngine(db_path=tmp_db.name, provider=_FailingProvider()) as engine:
            # Verify ContextBuilder has loaded snapshot
            system_content = engine.context_builder._build_system_content()
            assert "WRITE_FILE|" not in system_content
            assert "RUN_SHELL|" not in system_content
            # USER.md profile, MEMORY.md or AGENTS.md content check
            assert "Antigona" in system_content


@pytest.mark.asyncio
async def test_dialogue_engine_persists_turns_to_db() -> None:
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as tmp_db:
        repo = SessionRepository(db_path=tmp_db.name)
        await repo.connect()
        try:
            engine = DialogueEngine(repository=repo, provider=_FailingProvider())
            sess_id = "test-session-123"

            reply1 = await engine.reply("Привет!", session_id=sess_id)
            assert isinstance(reply1, str)
            assert len(reply1) > 0

            # Check DB messages
            messages = await repo.get_messages(sess_id)
            assert len(messages) == 2
            assert messages[0]["role"] == "user"
            assert messages[0]["content"] == "Привет!"
            assert messages[1]["role"] == "assistant"
            assert messages[1]["content"] == reply1

            # Second turn
            reply2 = await engine.reply("Кто я?", session_id=sess_id)
            assert "Владелец" in reply2

            messages_after = await repo.get_messages(sess_id)
            assert len(messages_after) == 4
            assert messages_after[2]["content"] == "Кто я?"
            assert messages_after[3]["content"] == reply2
        finally:
            await repo.close()


@pytest.mark.asyncio
async def test_dialogue_engine_natural_replies_without_taskflow() -> None:
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as tmp_db:
        repo = SessionRepository(db_path=tmp_db.name)
        await repo.connect()
        try:
            # Hermetic: failing stub provider forces deterministic fallback.
            engine = DialogueEngine(repository=repo, provider=_FailingProvider())
            sess_id = "natural-test-session"

            # 1. "Кто я?"
            res_who = await engine.reply("Кто я?", session_id=sess_id)
            assert "Владелец" in res_who

            # 2. "Да"
            res_yes = await engine.reply("Да", session_id=sess_id)
            assert any(w in res_yes.lower() for w in ("отлично", "помочь", "хорошо", "слушаю"))

            # 3. "Нет"
            res_no = await engine.reply("Нет", session_id=sess_id)
            assert any(w in res_no.lower() for w in ("поняла", "обращайтесь", "хорошо"))

            # 4. "Продолжай"
            res_cont = await engine.reply("Продолжай", session_id=sess_id)
            assert any(w in res_cont.lower() for w in ("слушаю", "дальше", "продолжаю"))

            # 5. "О чём мы говорили?"
            res_summary = await engine.reply("О чём мы говорили?", session_id=sess_id)
            assert "говорили" in res_summary.lower() or "владелец" in res_summary.lower()
        finally:
            await repo.close()


@pytest.mark.asyncio
async def test_dialogue_engine_uses_provider_when_available() -> None:
    """Если provider жив — engine возвращает его ответ (а не fallback)."""
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as tmp_db:
        repo = SessionRepository(db_path=tmp_db.name)
        await repo.connect()
        try:
            engine = DialogueEngine(repository=repo, provider=_FixedProvider("Привет, Владелец!"))
            reply = await engine.reply("Привет!", session_id="provider-session")
            assert reply == "Привет, Владелец!"
        finally:
            await repo.close()


@pytest.mark.asyncio
async def test_chat_controller_integration() -> None:
    """Тонкий CLI-клиент: free text идёт через Turn API, а не локальный engine."""
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as tmp_db:
        repo = SessionRepository(db_path=tmp_db.name)
        await repo.connect()
        try:
            mock_gateway = MagicMock()

            async def _fake_turn(
                text: str,
                session_id: str,
                channel: str = "cli",
                user_id: str = "default",
                turn_id: str = "",
            ) -> dict[str, Any]:
                return {
                    "reply": "Вы — Владелец, создатель и главный разработчик Antigona.",
                    "session_id": session_id,
                    "verified": None,
                    "response_type": "conversation",
                    "flow_id": None,
                    "requires_approval": False,
                }

            mock_gateway.send_dialogue_turn = _fake_turn
            controller = ChatController(
                gateway=mock_gateway,
                conversation_id="controller-test-sess",
                enable_animations=False,
            )

            # Process chitchat through the Turn API (thin client)
            disp = await controller.handle_input("Кто я?")
            assert disp.name == "LOCAL_ACTION"
            assert len(controller.state.messages) == 2  # user + assistant
            assert str(controller.state.messages[1].role) == "assistant"
            assert "Владелец" in controller.state.messages[1].content
        finally:
            await repo.close()


# ── FAILURE C/D (MASTER LOOP ENGINEERING v2.1, guio.md) ────────────────────
# C: финальная доставка обязана использовать verified result, НИКОГДА
#    оригинальный запрос пользователя (если задача не просила echo).
# D: история разговора может использоваться для рассуждения, но НЕ может
#    становиться финальным ответом, если пользователь не просил её показать.
# Здесь проверяется детерминированный fallback-путь (LLM недоступен): он НЕ
# должен возвращать оригинальный user-запрос / историю как ответ.


@pytest.mark.asyncio
async def test_failure_cd_fallback_never_echoes_original_request() -> None:
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as tmp_db:
        repo = SessionRepository(db_path=tmp_db.name)
        await repo.connect()
        try:
            engine = DialogueEngine(repository=repo, provider=_FailingProvider())
            sess_id = "failure-cd-echo-test"

            # Предыдущий ход владельца — из него fallback мог бы взять «проект»/«цвет».
            original = "Мой любимый проект называется Nebula, любимый цвет — синий"
            await engine.reply(original, session_id=sess_id)

            # Вопрос о любимом проекте/цвете при недоступном LLM.
            reply = await engine.reply(
                "Какой у меня любимый цвет?", session_id=sess_id
            )
            # Ответ НЕ должен быть эхом оригинального запроса как «факт».
            assert original not in reply, (
                "FAILURE C/D: fallback вернул оригинальный запрос как ответ"
            )
            assert "Nebula" not in reply or "Судя по нашему разговору" not in reply, (
                "FAILURE C/D: история превратилась в финальный ответ"
            )
        finally:
            await repo.close()


@pytest.mark.asyncio
async def test_failure_cd_fallback_never_dumps_history() -> None:
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as tmp_db:
        repo = SessionRepository(db_path=tmp_db.name)
        await repo.connect()
        try:
            engine = DialogueEngine(repository=repo, provider=_FailingProvider())
            sess_id = "failure-cd-history-dump"

            prev = "Прошлое сообщение: ANTIGONA FILE TEST\n12345\nЕщё строка"
            await engine.reply(prev, session_id=sess_id)

            # Запрос, триггерящий context-retention ветку («проект», «цвет»).
            reply = await engine.reply("Какой проект?", session_id=sess_id)

            # Полный дамп истории/оригинала как финальный ответ запрещён.
            assert "ANTIGONA FILE TEST" not in reply, (
                "FAILURE D: история разговора стала финальным ответом"
            )
        finally:
            await repo.close()


@pytest.mark.asyncio
async def test_dialogue_engine_owner_name_is_configurable_via_env(monkeypatch) -> None:
    """Owner identity in fallback replies is env-configurable, neutral by default.

    Proves the remediation: the owner name is read from ANTIGONA_OWNER_NAME
    (no hard-coded personal name), and falls back to a neutral role label when
    unset.
    """
    from antigona.context.builder import owner_name

    # Neutral default when env unset.
    assert owner_name() == "Владелец"

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp_db:
        repo = SessionRepository(db_path=tmp_db.name)
        await repo.connect()
        try:
            engine = DialogueEngine(repository=repo, provider=_FailingProvider())

            # Custom configured owner name is honoured.
            monkeypatch.setenv("ANTIGONA_OWNER_NAME", "CustomOwner")
            reply = await engine.reply("Кто я?", session_id="cfg-session")
            assert "CustomOwner" in reply

            # Unset → neutral fallback (no personal name).
            monkeypatch.delenv("ANTIGONA_OWNER_NAME")
            reply2 = await engine.reply("Кто я?", session_id="cfg-session")
            assert "Владелец" in reply2
        finally:
            await repo.close()
