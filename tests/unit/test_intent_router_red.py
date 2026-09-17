"""RED-тесты смысловой классификации (требования владельца, Stage 1).

«Кто я?», «Кто ты?», «Привет! Как дела?» и обычные вопросы ДОЛЖНЫ быть
conversation — никогда clarification и никогда task. Это не ослабляемые
проверки: шаблонный ответ не доказывает работу разговора.
"""

from __future__ import annotations

import pytest

from antigona.router.intent_router import IntentRouter


@pytest.fixture()
def router() -> IntentRouter:
    return IntentRouter()


@pytest.mark.parametrize(
    "text",
    [
        "Кто я?",
        "Кто я такой?",
        "Кто я такая?",
    ],
)
def test_who_am_i_is_identity_conversation(router: IntentRouter, text: str) -> None:
    """«Кто я?» — вопрос о пользователе: conversation/question, не clarification."""
    decision = router.route(text=text, context={"source": "cli"})
    assert decision.intent.startswith("conversation.") or decision.intent.startswith("question.")
    assert decision.intent != "ambiguous.mixed_intent"
    assert decision.intent != "task."


@pytest.mark.parametrize(
    "text",
    [
        "Кто ты?",
        "Кто ты такая?",
        "Ты кто?",
        "Что ты такое?",
    ],
)
def test_who_are_you_is_persona_conversation(router: IntentRouter, text: str) -> None:
    """«Кто ты?» — persona conversation."""
    decision = router.route(text=text, context={"source": "cli"})
    assert decision.intent.startswith("conversation.")
    assert decision.intent != "ambiguous.mixed_intent"


@pytest.mark.parametrize(
    "text",
    [
        "Привет! Как дела?",
        "Привет, как дела?",
        "Здравствуйте! Как поживаете?",
    ],
)
def test_greeting_with_smalltalk_is_conversation(router: IntentRouter, text: str) -> None:
    """Приветствие с вопросом — conversation, не clarification и не task."""
    decision = router.route(text=text, context={"source": "cli"})
    assert decision.intent.startswith("conversation.")
    assert decision.intent != "ambiguous.mixed_intent"
    assert not decision.intent.startswith("task.")


@pytest.mark.parametrize(
    "text",
    [
        "Сколько будет 17 плюс 28?",
        "Что такое вектор?",
        "Почему небо голубое?",
        "Какая сегодня дата?",
        "Объясни, как работает queue",
    ],
)
def test_ordinary_question_is_not_clarification_or_task(router: IntentRouter, text: str) -> None:
    """Обычный вопрос не должен превращаться в clarification или task."""
    decision = router.route(text=text, context={"source": "cli"})
    # Answer-интенты: conversation.* / question.* / analysis.* (explain/inspect).
    assert decision.intent.startswith(("conversation.", "question.", "analysis."))
    assert decision.intent != "ambiguous.mixed_intent"
    assert not decision.intent.startswith("task.")


def test_brain_process_who_am_i_returns_conversation_reply() -> None:
    """Через brain: «Кто я?» даёт conversation-ответ (не clarification)."""
    import asyncio

    from antigona.core.brain import AntigonaBrain, ResponseType

    async def _run() -> str:
        brain = AntigonaBrain()
        await brain.connect()
        try:
            resp = await brain.process(
                text="Кто я?",
                user_id="test-user",
                channel="cli",
                session_id="red-whoami-test",
            )
            return resp.response_type
        finally:
            await brain.close()

    response_type = asyncio.run(_run())
    assert response_type == ResponseType.CONVERSATION


# ── FAILURE A (MASTER LOOP ENGINEERING v2.1, guio.md) — direct-response routing ──
# Чистые констрейнты (pure constraints) должны НИКОГДА не входить в task/approval
# pipeline: никакого task-создания, approval, tool-call, echo. Роутер обязан
# классифицировать их как conversation/answer. НЕ ослабляемо, НЕ special-case
# точных русских строк — правим классификатор.
_DIRECT_CONSTRAINT_CASES = [
    # Ответь ровно одним словом
    "Ответь ровно одним словом: ГОТОВО",
    "Ответь ровно одним числом: 17",
    # Назови N + ровно N строк
    "Назови три цвета. Ровно три строки. Не добавляй ничего лишнего.",
    "Назови три цвета",
    # Напиши значения без файлового маркера
    "Напиши число 17, затем слово TEST, затем число 42. Только эти три значения.",
    "Перечисли числа от 1 до 5",
    # format-constraint without file target
    "Выведи ровно одну строку: тест",
    # T03 live-fix: перечисление литералов «затем/запятой» без «ровно/только»
    "Напиши число 17, затем слово TEST, затем число 42. Всё в одной строке через пробел.",
    "Напиши число 17, затем слово TEST, затем число 42.",
    "Выведи число 1, затем слово A, затем число 2",
]


@pytest.mark.parametrize("text", _DIRECT_CONSTRAINT_CASES)
def test_pure_constraint_is_direct_not_task(router: IntentRouter, text: str) -> None:
    """Чистый констрейнт-запрос НЕ должен становиться task/approval."""
    decision = router.route(text=text, context={"source": "telegram"})
    # Never a task flow, never approval, never planner, never clarify-fallback.
    assert not decision.intent.startswith("task.")
    assert not decision.requires_approval
    assert not decision.requires_planner
    # Direct-response intents (conversation/answer/question/analysis).
    assert decision.intent.startswith(("conversation.", "question.", "analysis.", "answer"))
    # Do not dump/echo the original request as the whole answer path.
    assert decision.response_mode in ("conversation", "answer")


# ── FAILURE F (MASTER LOOP ENGINEERING v2.1, guio.md) — conversation reference ──
# «эту историю» / «этот текст» должны резолвиться на предшествующий assistant
# output, когда он доступен в состоянии диалога — а НЕ уходить в clarify.
# В частности, запрос с файловым target («сделай из этого текста файл X.txt»)
# обязан идти в task.file_write (контекст резолвится через draft с историей),
# а не в ambiguous/clarify из-за глагола «сделай из этого».
_FAILURE_F_REFERENCE_CASES = [
    # «этот текст» + явный файл → write-задача, не clarify
    "Сделай из этого текста файл doc.txt",
    "Сделай из этого текста файл output.md",
    # «этот текст» без файла → conversation (история резолвит контекст), не clarify
    "Сделай из этого текста документ",
]


@pytest.mark.parametrize("text", _FAILURE_F_REFERENCE_CASES)
def test_reference_to_prior_text_not_clarified(router: IntentRouter, text: str) -> None:
    """FAILURE F: отсылка к предшествующему тексту не должна требовать уточнения.

    С файловым target («...файл X.txt») → task.file_write (история резолвится в draft).
    Без файла → conversation.followup (не clarify, не task).
    """
    decision = router.route(text=text, context={"source": "telegram", "active_topic": "предыдущий текст"})
    assert decision.intent != "ambiguous.mixed_intent", (
        f"FAILURE F: reference clarified instead of resolving context: {decision.reason_code}"
    )
    assert decision.response_mode != "clarify"


@pytest.mark.parametrize(
    "text",
    ["Сделай из этого текста файл doc.txt", "Сделай из этого текста файл output.md"],
)
def test_reference_with_file_target_is_task_file_write(
    router: IntentRouter, text: str
) -> None:
    """FAILURE F + файл: «...файл X.txt» — task.file_write, не clarify и не task.shell."""
    decision = router.route(text=text, context={"source": "telegram"})
    assert decision.intent == "task.file_write"
    assert decision.requires_approval
    assert "path" in decision.entities


@pytest.mark.parametrize(
    "text",
    [
        # FAILURE E — явная файловая задача ОСТАЁТСЯ task (должна быть перехвачена
        # task.file_write, а НЕ direct-format): файловый маркер «файл X.txt» / «в файл».
        "Создай файл literal.txt. Содержимое должно быть ровно двумя строками: ANTIGONA FILE TEST 12345",
        "Запиши в файл out.txt ровно две строки: ANTIGONA FILE TEST",
        "Создай в своём разрешённом workspace файл manual_test.txt. Запиши туда ровно две строки: ANTIGONA FILE TEST 12345",
    ],
)
def test_file_write_task_stays_task(router: IntentRouter, text: str) -> None:
    """Явное создание файла (FAILURE E) остаётся task.file_write — direct-детектор
    НЕ должен красть его у task-пайплайна."""
    decision = router.route(text=text, context={"source": "telegram"})
    assert decision.intent == "task.file_write"
    assert decision.requires_approval

