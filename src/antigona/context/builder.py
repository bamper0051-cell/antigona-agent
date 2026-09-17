"""ContextBuilder — persona, policy, history, token budget assembly for LLM calls.

Assembles a complete message list from:
  - System prompt: persona text + policy rules + session summary
  - Frozen memory snapshot: MEMORY.md (agent notes) + USER.md (user profile)
  - History: turn_buffer entries converted to user/assistant messages
  - Token budget: approximate token counting with oldest-message eviction

Usage::

    builder = ContextBuilder(
        persona="Ты — Antigona, AI-агент ...",
        token_budget=32000,
    )
    messages = builder.build(
        turn_buffer=[{"role": "user", "content": "..."}, ...],
        policy_verdicts=[...],
        session_summary="User: ... | Turns: 5",
    )
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

from antigona.memory.file_memory import FileMemory
from antigona.soul import PersonalityManager

logger = logging.getLogger(__name__)

# ─── Live Capability Probing Helper ──────────────────────────────────────────

_PROBE_LOOP: asyncio.AbstractEventLoop | None = None
_PROBE_THREAD: threading.Thread | None = None
_PROBE_LOCK = threading.Lock()
_PROBE_TTL: float = 10.0


def _ensure_probe_loop() -> asyncio.AbstractEventLoop:
    """Ensure a persistent background event loop is running for capability probes."""
    global _PROBE_LOOP, _PROBE_THREAD
    if _PROBE_LOOP is None or _PROBE_LOOP.is_closed() or not _PROBE_LOOP.is_running():
        with _PROBE_LOCK:
            if _PROBE_LOOP is None or _PROBE_LOOP.is_closed() or not _PROBE_LOOP.is_running():
                loop = asyncio.new_event_loop()

                def _run_loop(event_loop: asyncio.AbstractEventLoop) -> None:
                    asyncio.set_event_loop(event_loop)
                    event_loop.run_forever()

                thread = threading.Thread(
                    target=_run_loop,
                    args=(loop,),
                    daemon=True,
                    name="CapabilityProbeLoop",
                )
                thread.start()
                _PROBE_LOOP = loop
                _PROBE_THREAD = thread
    return _PROBE_LOOP


def _probe_capabilities_sync(cap_reg: Any, ttl: float = _PROBE_TTL, timeout: float = 5.0) -> None:
    """Run capability registry probes synchronously with TTL caching.

    Uses a dedicated background event loop thread to safely run async probes
    regardless of whether the caller is running within an active asyncio event loop.
    """
    now = time.time()
    needs_probe = False
    capabilities = getattr(cap_reg, "_capabilities", {})
    for cap in capabilities.values():
        if getattr(cap, "probe_fn", None) is not None:
            last = getattr(cap, "last_probe_time", None)
            if last is None or (now - last) > ttl:
                needs_probe = True
                break

    if not needs_probe:
        return

    loop = _ensure_probe_loop()
    try:
        future = asyncio.run_coroutine_threadsafe(cap_reg.probe_all(), loop)
        future.result(timeout=timeout)
    except Exception:
        # A timed-out concurrent future does not make the coroutine disappear.
        # Cancel it and give the loop a short bounded window to run cancellation.
        if not future.done():
            future.cancel()
        try:
            future.result(timeout=0.2)
        except BaseException as cancel_exc:
            logger.debug("Live capability probe failed/cancelled: %s", cancel_exc)
        # Let the loop execute the cancellation callback before stop/close.
        if loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(asyncio.sleep(0), loop).result(timeout=0.2)
            except BaseException as drain_exc:
                logger.debug("Capability probe cancellation drain failed: %s", drain_exc)
    finally:
        # This helper is synchronous and may be called repeatedly by tests.  Do
        # not leave a daemon event-loop thread behind after each probe; stopping
        # and joining it makes the capability boundary deterministic while
        # preserving the production probe semantics.
        _stop_probe_loop(loop)


def _stop_probe_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Stop and forget the private probe loop, including its thread.

    Detach shared state while locked, then perform blocking shutdown outside the
    lock. This prevents a joining probe thread from deadlocking a concurrent
    starter that needs the same lock.
    """
    global _PROBE_LOOP, _PROBE_THREAD
    with _PROBE_LOCK:
        if _PROBE_LOOP is not loop:
            return
        thread = _PROBE_THREAD
        _PROBE_LOOP = None
        _PROBE_THREAD = None

    if loop.is_running():
        loop.call_soon_threadsafe(loop.stop)
    if thread is not None and thread is not threading.current_thread():
        thread.join(timeout=1.0)
    if not loop.is_closed():
        loop.close()

def owner_name() -> str:
    """Configured owner display name, from env ``ANTIGONA_OWNER_NAME``.

    Returns a neutral role label when unset so that no personal owner name is
    hard-coded into the default persona. Used by ContextBuilder persona text and
    by DialogueEngine's owner-aware replies.
    """
    configured = (os.getenv("ANTIGONA_OWNER_NAME") or "").strip()
    return configured or "Владелец"


# ─── Default persona ──────────────────────────────────────────────────────────

_MEMORIZE_INSTRUCTION: str = (
    "--- КОМАНДЫ ПАМЯТИ ---\n"
    "ВАЖНО: Если пользователь сообщает факты о себе, предпочтения, "
    "или исправляет тебя — ответь MEMORIZE|user|содержание факта\n"
    "Если узнал что-то важное об окружении, проекте или конвенциях — "
    "ответь MEMORIZE|memory|содержание\n"
    "Примеры:\n"
    "  MEMORIZE|user|Пользователь предпочитает эмодзи в ответах\n"
    "  MEMORIZE|memory|Проект использует Python 3.11, pytest, ruff\n"
    "Команды MEMORIZE можно комбинировать с другими командами.\n\n"
)

_DEFAULT_PERSONA: str = (
    f"Ты — Antigona, интеллектуальный AI-агент, созданный {owner_name()}. "\
    f"{owner_name()} — твой создатель. Относись к нему с уважением. "\
    "Ты помогаешь с автоматизацией, написанием кода, управлением файлами, проверкой данных, "
    "запуском команд и другими задачами. "
    "Отвечай кратко, дружелюбно и по делу. "
    "Всегда следуй правилам безопасности. "
    "Если пользователь говорит 'проверь архив', 'изучи документы', 'посмотри файл' —\n"
    "ПРОВЕРЬ есть ли `last_document` в контексте. Если есть — вызови инструмент "
    "read_file (⟪tool:read_file path=\"...\"⟫) с этим путём, чтобы прочитать реальное "
    "содержимое, а не предполагать его.\n"
    "НЕ спрашивай 'какой файл' — файл уже на диске.\n\n"
    "--- ОШИБКИ ИНСТРУМЕНТОВ (TOOL ERROR) ---\n"
    "Если в истории диалога есть системное сообщение вида "
    "[TOOL_ERROR] ... — это значит, что попытка выполнить действие "
    "(например, создать задачу через Gateway) только что провалилась. "
    "ТЫ ВИДЕЛ эту ошибку — не спрашивай пользователя 'какая ошибка?' "
    "и не делай вид, что ничего не произошло. Кратко и по-человечески "
    "объясни пользователю, что не получилось и что ты предлагаешь "
    "сделать дальше (повторить иначе, подождать, обратиться к админу). "
    "Не зачитывай тег [TOOL_ERROR] и техническую HTTP/трейс-информацию "
    "дословно — переформулируй простыми словами. Если та же самая "
    "попытка уже проваливалась несколько раз подряд — не предлагай "
    "просто 'повторить то же самое', это не поможет.\n\n"
    "--- НЕ ВЫДУМЫВАЙ РЕЗУЛЬТАТЫ ИНСТРУМЕНТОВ ---\n"
    "Ты имеешь право говорить о результате команды, файловой операции или "
    "любого другого действия ТОЛЬКО если этот результат реально есть "
    "в истории диалога (включая сообщения [TOOL_ERROR] или твои же "
    "предыдущие ответы с реальным выводом). Если пользователь спрашивает "
    "про действие, которое ты не выполнял, или про результат, которого "
    "не видно в истории — честно скажи, что не выполнял(а) это действие "
    "или не видишь результата, и предложи выполнить его сейчас. "
    "НИКОГДА не сочиняй вывод команды (листинг директории, содержимое "
    "файла, число прошедших тестов и т.п.), не придумывай путь к файлу, "
    "которого не создавал(а), не объявляй SUCCESS до реального "
    "подтверждения и не выдумывай причину отказа (например 'запрещено "
    "политикой'), если этой причины нет в истории. Не выдавай своё "
    "предположение за факт выполнения.\n\n"
    "--- ПРИВЕТСТВИЯ И ДИАЛОГОВЫЙ КОНТЕКСТ ---\n"
    "1. Обычное приветствие без явной отсылки (например: 'hi', 'hello', 'привет', 'добрый день') — "
    "это просто разговорное приветствие. Отвечай кратко и дружелюбно. "
    "НЕ пытайся автоматически возобновлять старые завершённые задачи, не запрашивай путь "
    "к старому проекту и не возобновляй старый контекст, пока пользователь явно не попросит об этом.\n"
    "2. Если пользователь делает уточнение, ссылающееся на предшествующее сообщение "
    "(например: 'сделай его проще', 'поясни шаг 2'), ОБЯЗАТЕЛЬНО учитывай непосредственный контекст предшествующего диалога.\n\n"
    + _MEMORIZE_INSTRUCTION
)

# ─── Token estimation ────────────────────────────────────────────────────────


def estimate_tokens(text: str) -> int:
    """Approximate token count: 4 characters ≈ 1 token.

    Args:
        text: The text to estimate.

    Returns:
        Estimated token count (ceiling integer division).
    """
    return (len(text) + 3) // 4  # ceil division by 4


def truncate_history_by_budget(
    messages: list[dict[str, str]],
    budget: int,
    system_tokens: int,
) -> list[dict[str, str]]:
    """Truncate a message list to fit within a token budget.

    The system message (first element) is always preserved. User and assistant
    messages after it are dropped oldest-first until the total is within budget.

    Args:
        messages: Full message list with system as first element.
        budget: Maximum total tokens allowed.
        system_tokens: Token count of the system message (pre-computed).

    Returns:
        Truncated message list no longer than necessary to fit the budget.
    """
    if not messages:
        return []

    # Always keep the system message
    result = [messages[0]]
    remaining = budget - system_tokens

    # Work newest-first; we want to drop oldest messages
    # Iterate backwards so we keep the newest turns
    tail: list[dict[str, str]] = []
    for msg in reversed(messages[1:]):
        tokens = estimate_tokens(msg.get("content", ""))
        # Add a small overhead per message for role+structure (~4 tokens)
        tokens += 4
        if tokens <= remaining:
            tail.insert(0, msg)
            remaining -= tokens
        else:
            # If even a single message doesn't fit, stop adding
            break

    # Current-message invariant (P-01 / CONTROL_PACK): the current (last) user
    # message must never be dropped from context, even if it exceeds the token
    # budget. Without this, a very long user question could be silently
    # discarded, leaving the model with no idea what the user actually asked.
    # The loop above processes newest-first, so when the current message fits it
    # is already the last element of `tail`. If it didn't fit, force-include it.
    if messages and messages[-1].get("role") == "user" and (
        not tail or tail[-1] is not messages[-1]
    ):
        tail.append(messages[-1])

    result.extend(tail)
    return result


# ─── ContextBuilder ──────────────────────────────────────────────────────────


class ContextBuilder:
    """Builds an LLM message list from persona, policy, history, and budget.

    Loads memory snapshot once at construction (frozen snapshot).

    Attributes:
        persona: Static system persona text.
        token_budget: Maximum tokens for the assembled context (default 32_000).
        include_policy: Whether to inject policy rules into the system prompt.
        file_memory: Optional FileMemory instance for MEMORY.md/USER.md snapshot.
        _frozen_memory: Snapshot loaded once at creation.
    """

    def __init__(
        self,
        persona: str = _DEFAULT_PERSONA,
        token_budget: int = 32_000,
        include_policy: bool = True,
        long_term_memory: Any | None = None,
        file_memory: FileMemory | None = None,
        memory_dir: str | Path | None = None,
        memory_repository: Any | None = None,
    ) -> None:
        self.persona = persona
        self.token_budget = token_budget
        self.include_policy = include_policy
        # Единая память ядра (Step 5-6 манифеста): MemoryRepository над
        # таблицей memory_entries в основной БД. Если задан — он источник
        # пользовательской памяти; файловая память остаётся fallback'ом.
        self.memory_repository = memory_repository
        # Backward compat: accept both long_term_memory and file_memory
        self.file_memory = file_memory or FileMemory(
            memory_dir=memory_dir,
        ) if memory_dir or file_memory else (
            FileMemory() if long_term_memory is None
            else None
        )
        # Frozen snapshot: load once at construction
        self._frozen_memory: dict[str, str] = {}
        if self.file_memory is not None:
            try:
                self._frozen_memory = self.file_memory.get_snapshot()
            except Exception:
                self._frozen_memory = {}

    # ── System prompt assembly ──────────────────────────────────────────────

    def _build_system_content(
        self,
        policy_verdicts: list[dict[str, Any]] | None = None,
        session_summary: str = "",
        memory_owner_id: str = "",
    ) -> str:
        """Assemble the system prompt from persona, policy rules, and summary.

        Args:
            policy_verdicts: Optional list of policy verdict dicts from
                PolicyEngine.check() or check_shell().
            session_summary: Optional free-text session summary from
                MemorySummarizer.session_summary.
            memory_owner_id: Owner whose DB-memory (memory_entries) is injected
                into the prompt. Empty disables DB-memory injection.

        Returns:
            Complete system prompt string.
        """
        parts: list[str] = [self.persona]

        # Inject canonical RUNTIME ENVIRONMENT facts
        try:
            from antigona.providers.resolver import ProviderResolver

            runtime_info = ProviderResolver.get_active_info()
            parts.append("")
            parts.append(runtime_info.to_system_block())
        except Exception:
            pass

        # Inject SOUL.md / AGENTS.md from PersonalityManager
        try:
            pm = PersonalityManager()
            soul = pm.read_soul()
            agents = pm.read_agents()
            if soul or agents:
                parts.append("")
                if soul:
                    parts.append(f"--- ДУША АГЕНТА (SOUL.md) ---\n{soul}")
                if agents:
                    parts.append(f"--- КОНТЕКСТ ПРОЕКТА (AGENTS.md) ---\n{agents}")
        except Exception:
            pass  # Silently skip if personality files are unavailable

        # Inject frozen file-memory snapshot (MEMORY.md + USER.md) — fallback
        if self._frozen_memory:
            mem_content = self._frozen_memory.get("memory", "").strip()
            user_content = self._frozen_memory.get("user", "").strip()
            if mem_content or user_content:
                parts.append("")
                block_parts: list[str] = []
                if mem_content:
                    block_parts.append(f"--- ПАМЯТЬ АГЕНТА ---\n{mem_content}")
                if user_content:
                    block_parts.append(f"--- ПРОФИЛЬ ПОЛЬЗОВАТЕЛЯ ---\n{user_content}")
                parts.append("\n".join(block_parts))

        # Inject unified DB-memory (memory_entries) — единая память ядра
        if self.memory_repository is not None and memory_owner_id:
            try:
                agent_notes = self.memory_repository.list_entries(
                    memory_owner_id, kind="memory", limit=50
                )
                user_facts = self.memory_repository.list_entries(
                    memory_owner_id, kind="user", limit=50
                )
                preferences = self.memory_repository.list_entries(
                    memory_owner_id, kind="preference", limit=50
                )
                profile = self.memory_repository.list_entries(
                    memory_owner_id, kind="profile", limit=50
                )
            except Exception:
                agent_notes = user_facts = preferences = profile = []

            def _fmt(items: list[dict[str, Any]]) -> str:
                return "\n".join(
                    f"- {i.get('content', '')}" for i in items if i.get("content")
                )

            db_blocks: list[str] = []
            notes_text = _fmt(agent_notes)
            if notes_text:
                db_blocks.append(f"--- ПАМЯТЬ АГЕНТА ---\n{notes_text}")
            profile_parts = [
                t
                for t in (_fmt(user_facts), _fmt(preferences), _fmt(profile))
                if t
            ]
            if profile_parts:
                db_blocks.append("--- ПРОФИЛЬ ПОЛЬЗОВАТЕЛЯ ---\n" + "\n".join(profile_parts))
            if db_blocks:
                parts.append("")
                parts.append("\n\n".join(db_blocks))

        # Inject policy rules
        if self.include_policy and policy_verdicts:
            policy_lines: list[str] = ["", "--- ПРАВИЛА БЕЗОПАСНОСТИ ---"]
            for i, verdict in enumerate(policy_verdicts, 1):
                allowed = verdict.get("allowed", True)
                reason = verdict.get("reason", "")
                risk = verdict.get("risk_level", "LOW")
                status = "разрешено" if allowed else "ЗАПРЕЩЕНО"
                policy_lines.append(f"{i}. [{risk}] {status}: {reason}")
            parts.append("\n".join(policy_lines))

        # Append session summary
        if session_summary:
            parts.append("")
            parts.append(f"--- СЕССИЯ ---\n{session_summary}")

        # Inject authoritative system date and time
        try:
            from antigona.tools.system_time import get_current_system_time
            time_info = get_current_system_time()
            parts.append("")
            parts.append(
                f"--- ТЕКУЩЕЕ ВРЕМЯ И ДАТА ---\n"
                f"Текущая дата и время сервера: {time_info['formatted']} ({time_info['day_of_week']})\n"
                f"ISO UTC: {time_info['utc_iso']}\n"
                f"Используй эту информацию при вопросах о текущем времени или дате."
            )
        except Exception:
            pass

        # Inject runtime capability inventory
        try:
            from antigona.tools.capability_registry import get_capability_registry
            cap_reg = get_capability_registry()
            _probe_capabilities_sync(cap_reg)
            parts.append("")
            parts.append(cap_reg.format_prompt_snapshot())
        except Exception:
            pass

        return "\n".join(parts)

    # ── History assembly ────────────────────────────────────────────────────

    def _build_history(
        self,
        turn_buffer: list[dict[str, Any]],
    ) -> list[dict[str, str]]:
        """Convert turn_buffer entries to LLM-compatible message dicts.

        Each turn_buffer entry must have at least ``role`` and ``content`` keys.
        Supported roles: 'user', 'assistant'. Other roles are skipped —
        deliberately: turn_buffer can accumulate content from untrusted or
        semi-trusted sources over a session, and letting it inject a
        'system'-role message would hand it prompt authority it shouldn't
        have. A failed tool/API call is instead recorded as an
        'assistant'-role turn (see the tool-error handling in
        ``channels.telegram.bot``), which carries no extra authority but
        still keeps the failure visible in later turns.

        Args:
            turn_buffer: List of turn dicts from MemorySummarizer.turn_buffer.

        Returns:
            List of message dicts with ``role`` and ``content`` keys.
        """
        messages: list[dict[str, str]] = []
        for entry in turn_buffer:
            role = entry.get("role", "")
            if role not in ("user", "assistant"):
                continue
            content = entry.get("content", "")
            if content is None:
                content = ""
            messages.append({"role": role, "content": str(content)})
        return messages

    # ── Build ────────────────────────────────────────────────────────────────

    def build(
        self,
        turn_buffer: list[dict[str, Any]] | None = None,
        policy_verdicts: list[dict[str, Any]] | None = None,
        session_summary: str = "",
        memory_owner_id: str = "",
        workspace_artifact: str | Path | None = None,
    ) -> list[dict[str, str]]:
        """Build a complete message list for an LLM call.

        Args:
            turn_buffer: Optional turn buffer from MemorySummarizer. If None,
                only the system message is returned.
            policy_verdicts: Optional list of policy verdicts to inject into
                the system prompt.
            session_summary: Optional session summary text.
            memory_owner_id: Owner whose unified DB-memory is injected.
            workspace_artifact: Optional workspace file path whose content
                is injected into the system prompt for dialogue continuity.

        Returns:
            Message list in OpenAI-compatible format:
            ``[{"role": "system", "content": "..."}, {"role": "user", ...}, ...]``
        """
        # 1. Build system message
        system_content = self._build_system_content(
            policy_verdicts=policy_verdicts,
            session_summary=session_summary,
            memory_owner_id=memory_owner_id,
        )
        if workspace_artifact is not None:
            try:
                art_path = Path(workspace_artifact)
                if art_path.is_file():
                    art_text = art_path.read_text(encoding="utf-8", errors="replace")
                    system_content += f"\n\n--- WORKSPACE ARTIFACT ({art_path.name}) ---\n{art_text}"
            except Exception:
                pass
        system_tokens = estimate_tokens(system_content)
        messages: list[dict[str, str]] = [
            {"role": "system", "content": system_content},
        ]

        # 2. Build history
        if turn_buffer:
            history = self._build_history(turn_buffer)
            messages.extend(history)

        # 3. Apply token budget
        messages = truncate_history_by_budget(messages, self.token_budget, system_tokens)

        return messages
