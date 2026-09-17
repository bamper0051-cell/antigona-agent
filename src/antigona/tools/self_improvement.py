"""Self-improvement background review — периодический анализ бесед и обновление памяти.

Запускается через cron, анализирует последние N сообщений чата,
извлекает паттерны/предпочтения/уроки и сохраняет в MEMORY.md/USER.md.
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)

_SELF_IMPROVEMENT_PROMPT: str = (
    "Ты — система фонового самообучения Antigona. "
    "Твоя задача — проанализировать последние сообщения пользователя, "
    "извлечь факты о пользователе, его предпочтениях, проекте и окружении. "
    "Если нашёл что-то новое или важное — верни MEMORIZE|memory|факт или MEMORIZE|user|факт. "
    "Если ничего нового — верни [SILENT].\n\n"
    "Правила:\n"
    "- Сохраняй ТОЛЬКО durable факты (предпочтения, конфиги, уроки)\n"
    "- НЕ сохраняй временные/сессионные детали\n"
    "- Не дублируй то что уже в памяти\n"
    "- Используй русский язык для содержимого\n\n"
    "Сообщения пользователя:\n{conversation}"
)


async def run_self_improvement_review(
    chat_id: int,
    recent_messages: list[dict[str, str]],
    file_memory: Any,
    llm_provider: Any,
) -> str:
    """Run a background self-improvement review.

    Args:
        chat_id: Telegram chat ID.
        recent_messages: List of {role, content} dicts from recent conversation.
        file_memory: FileMemory instance for saving facts.
        llm_provider: LLM provider for analysis.

    Returns:
        Status message or empty string if nothing changed.
    """
    if not recent_messages:
        return ""

    # Build conversation text
    conv_text = "\n".join(
        f"{m.get('role', 'user')}: {m.get('content', '')[:500]}"
        for m in recent_messages[-10:]  # Last 10 messages
    )

    prompt = _SELF_IMPROVEMENT_PROMPT.format(conversation=conv_text)

    try:
        response = llm_provider.generate(prompt, max_tokens=500)
    except Exception as e:
        logger.warning(f"Self-improvement LLM failed: {e}")
        return ""

    response = (response or "").strip()

    if not response or response.upper().strip() == "[SILENT]":
        return ""

    # Parse MEMORIZE commands
    from antigona.tools.action_executor import ActionExecutor, ActionType

    executor = ActionExecutor()
    actions = executor.parse_action_from_llm(response)
    memorize_actions = [a for a in actions if a.type == ActionType.MEMORIZE]

    if not memorize_actions:
        return ""

    saved = 0
    for action in memorize_actions:
        try:
            store = action.metadata.get("store", "memory")
            title = action.metadata.get("title", action.content[:50].strip())
            file_memory.add_entry(store, title, action.content)
            saved += 1
        except ValueError:
            continue

    if saved:
        msg = f"🧠 Self-improvement: {saved} факт(ов) сохранено"
        logger.info(msg)
        return msg

    return ""
