"""DialogueEngine — Canonical conversation layer for Antigona.

Integrates ContextBuilder (USER.md, MEMORY.md, AGENTS.md), SessionRepository
(persistence in antigona_sessions.db), clean prompts without legacy Telegram tags,
and natural conversational responses without TaskFlow creation.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from antigona.context.builder import ContextBuilder, owner_name
from antigona.providers.base import BaseProvider
from antigona.sessions.repository import SessionRepository

logger = logging.getLogger(__name__)

__all__ = [
    "DRAFT_OK",
    "DRAFT_REJECTED",
    "DRAFT_UNAVAILABLE",
    "DialogueEngine",
    "FileContentDraft",
    "clean_telegram_tags",
    "extract_request_literals",
    "looks_like_clarification",
    "looks_like_tool_protocol_markup",
    "validate_draft_literals",
]

# Статусы черновика содержимого файла (LOOP3, DEFECT 3).
DRAFT_OK = "ok"
DRAFT_UNAVAILABLE = "unavailable"   # провайдера нет / провайдер недоступен
DRAFT_REJECTED = "rejected"         # провайдер ответил, но вывод непригоден

# Reasoning-модели тратят бюджет на thoughts. Черновик файла — one-shot
# генерация тела, ему нужен запас и на reasoning, и на content.
DRAFT_MAX_TOKENS = 4096


@dataclass(frozen=True)
class FileContentDraft:
    """Результат генерации содержимого файла.

    Attributes:
        content: Тело файла или ``None``, если черновика нет.
        status: Один из ``DRAFT_OK`` / ``DRAFT_UNAVAILABLE`` / ``DRAFT_REJECTED``.
    """

    content: str | None
    status: str

# Маркеры уточняющего ответа модели. Если черновик содержимого файла —
# это вопрос к владельцу, а не текст файла, его НЕЛЬЗЯ записывать на диск.
_CLARIFICATION_MARKERS = (
    "уточни",
    "уточните",
    "укажите",
    "какой именно",
    "какие именно",
    "что именно",
    "не понял",
    "не ясно",
    "непонятно",
    "please specify",
    "could you clarify",
    "clarify",
)

# Короткие ответы (до этого числа непустых строк) проверяются на маркеры
# уточнения. Длинный документ может законно содержать слово «укажите»,
# поэтому эвристика применяется только к коротким выводам.
_CLARIFICATION_MAX_LINES = 5


def looks_like_clarification(text: str) -> bool:
    """True, если вывод модели — просьба уточнить, а не содержимое файла.

    Вопрос («Какой текст записать в файл?») не должен становиться телом
    файла: это и есть корневой класс DEFECT 3 — артефакт, повторяющий
    инструкцию/вопрос вместо требуемого результата.
    """
    stripped = (text or "").strip()
    if not stripped:
        return True
    lines = [line for line in stripped.splitlines() if line.strip()]
    low = stripped.lower()
    # Целиком вопрос: короткий вывод, оканчивающийся вопросительным знаком,
    # либо начинающийся с него.
    if stripped.startswith("?"):
        return True
    if len(lines) <= 2 and stripped.endswith("?"):
        return True
    if len(lines) <= _CLARIFICATION_MAX_LINES and any(
        marker in low for marker in _CLARIFICATION_MARKERS
    ):
        return True
    return False


# Маркеры протокола вызова инструментов (XML/DSML). Модель, свалившаяся в
# tool-protocol, оборачивает требуемые литералы в служебную разметку — такой
# вывод НЕЛЬЗЯ записывать в файл, даже если литералы формально присутствуют.
_TOOL_PROTOCOL_MARKERS = (
    "<｜dsml｜",
    "</｜dsml｜",
    "<tool_call",
    "</tool_call>",
    "<invoke",
    "</invoke>",
    "<parameter",
    "</parameter>",
    "<function_call",
    "</function_call>",
)

# Ключи JSON-конвертов вызова функции/инструмента.
#: Substrings that mark a security denial (rather than a plain execution
#: failure) inside a serialized tool result.
_TOOL_DENIAL_MARKERS = (
    "fencing token",
    "ownership",
    "fence",
    "denied",
    "policy_denied",
    "not permitted",
    "refused",
)


#: A canonical voice-artifact marker the Telegram channel understands.
VOICE_MARKER_OPEN = "\u27ea"
VOICE_MARKER_CLOSE = "\u27eb"


def voice_marker_from_tool_result(result_text: str) -> str:
    """Rebuild a literal ``⟪voice:<path>⟫`` marker from a tool-result JSON.

    ``speech.tts`` returns its marker inside JSON, so ``json.dumps`` escapes the
    U+27EA/U+27EB brackets to ``\u27ea``/``\u27eb`` — the channel's delivery
    regex then never matches and the synthesized audio is silently NOT
    delivered. Rebuilding the marker from the raw ``audio_path`` field makes the
    delivered artifact match the promise (generation == delivery).
    """
    try:
        payload = json.loads(result_text)
    except Exception:
        return ""
    if not isinstance(payload, dict) or payload.get("error"):
        return ""
    raw_data = payload.get("data")
    data: dict[str, Any] = raw_data if isinstance(raw_data, dict) else {}
    path = str(data.get("audio_path") or "")
    if not path:
        for artifact in payload.get("artifacts") or []:
            if isinstance(artifact, dict) and artifact.get("type") == "audio":
                path = str(artifact.get("path") or "")
                if path:
                    break
    if not path:
        return ""
    return f"{VOICE_MARKER_OPEN}voice:{path}{VOICE_MARKER_CLOSE}"


def _tool_result_outcome(result_text: str) -> tuple[str, str | None]:
    """Classify a serialized tool result into a truth-contract outcome.

    Returns ``("SUCCEEDED", None)`` unless the payload explicitly reports a
    failure/denial, in which case the real error string is returned too.
    """
    low = result_text.lower()
    failed = (
        '"success": false' in low
        or '"success":false' in low
        or '"ok": false' in low
        or '"error"' in low
        or '"blocked": true' in low
        or "traceback" in low
    )
    if not failed:
        return "SUCCEEDED", None
    if any(marker in low for marker in _TOOL_DENIAL_MARKERS):
        return "DENIED", result_text[:500]
    return "FAILED", result_text[:500]


#: Tag prefix used by the conversation history grounding for a failed tool
#: (see ``core/brain.py`` direct-shell grounding).  An assistant message that
#: starts with it records a PREVIOUS turn's failure.
_TOOL_ERROR_TAG = "[TOOL_ERROR]"

#: Internal security wording that only ever originates from a fail-closed
#: error/denial — never from a legitimate conversational answer.
_INTERNAL_ERROR_MARKERS: tuple[str, ...] = (
    "fencing token",
    "ownership fence",
    "fail-closed",
    "fail closed",
    "denied_stale_fence",
    "stale fence",
    "write permit",
    "owner lease",
)

#: Neutral answer for a turn that executed NO tool.  Used when the model output
#: merely replays a previous turn's failure — never a failure/denial header.
_NEUTRAL_NO_ACTION_REPLY = (
    "В этом сообщении я ничего не выполняла — это обычный ответ. "
    "Уточните, что нужно сделать, и я выполню."
)


def _prior_tool_error_texts(turn_buffer: list[dict[str, Any]]) -> list[str]:
    """Error texts of PREVIOUS turns, taken from the recorded history."""
    errors: list[str] = []
    for entry in turn_buffer or []:
        if str(entry.get("role")) != "assistant":
            continue
        content = str(entry.get("content") or "")
        if content.startswith(_TOOL_ERROR_TAG):
            text = content[len(_TOOL_ERROR_TAG):].strip()
            if text:
                errors.append(text)
    return errors


def _contains_internal_error_marker(text: str) -> bool:
    low = (text or "").lower()
    return any(marker in low for marker in _INTERNAL_ERROR_MARKERS)


def _is_stale_error_replay(reply_text: str, prior_errors: list[str]) -> bool:
    """Whether *reply_text* merely replays a previous turn's failure text.

    Deterministic, narrow: only an assistant message that recorded a real
    failure is used, and the reply must reproduce it (verbatim containment either
    way) — an ordinary answer about the conversation is never flagged.
    """
    if not reply_text or not prior_errors:
        return False
    norm = " ".join(reply_text.split()).lower()
    if len(norm) < 20:
        return False
    for err in prior_errors:
        e = " ".join(err.split()).lower()
        if len(e) < 20:
            continue
        if e in norm or norm in e:
            return True
    return False


_TOOL_PROTOCOL_JSON_KEYS = ("tool_calls", "function")

# Регулярка нарратива выполненного инструмента: «Инструмент `имя` выполнен: ...».
_TOOL_RESULT_NARRATIVE_RE = re.compile(
    r"Инструмент\s*`\s*[\w.\-]+\s*`\s*выполнен\s*:",
    re.IGNORECASE,
)

# Ключи, сигнализирующие о inline JSON-РЕЗУЛЬТАТЕ выполнения инструмента
# (в отличие от JSON-конверта вызова, который покрывается выше).
_TOOL_RESULT_JSON_HINTS = ("size_bytes", "path", "content", "result", "output")


def looks_like_tool_protocol_markup(content: str) -> bool:
    """True, если вывод модели — служебный конверт вызова инструмента.

    Покрывает XML-разметку (``<tool_call>``, ``<invoke>``, ``<parameter>``),
    DSML-теги и JSON-конверты function/tool call. Литералы запроса внутри
    такого конверта не делают его пригодным телом файла.
    """
    stripped = (content or "").strip()
    if not stripped:
        return False
    low = stripped.lower()
    if any(marker in low for marker in _TOOL_PROTOCOL_MARKERS):
        return True
    # Нарратив выполненного инструмента (DEFECT tool-result-json-narrative):
    # «Инструмент `write_file` выполнен: {"success": true, ...}».
    if _TOOL_RESULT_NARRATIVE_RE.search(stripped):
        return True
    # Inline JSON-результат инструмента, вложенный в произвольный нарратив.
    if _contains_tool_result_json(stripped):
        return True
    if stripped.startswith("{") and stripped.endswith("}"):
        try:
            payload = json.loads(stripped)
        except ValueError:
            return False
        if isinstance(payload, dict):
            if "name" in payload and ("arguments" in payload or "parameters" in payload):
                return True
            if any(key in payload for key in _TOOL_PROTOCOL_JSON_KEYS):
                return True
    return False


def _iter_embedded_json_objects(text: str) -> Iterator[str]:
    """Yield complete JSON object literals embedded anywhere in ``text``."""
    search_from = 0
    while True:
        start = text.find("{", search_from)
        if start == -1:
            return
        depth = 0
        in_string = False
        escaped = False
        end = -1
        for idx in range(start, len(text)):
            ch = text[idx]
            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
            else:
                if ch == '"':
                    in_string = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        end = idx
                        break
        if end == -1:
            search_from = start + 1
            continue
        yield text[start : end + 1]
        search_from = end + 1


def _contains_tool_result_json(text: str) -> bool:
    """True, если внутри текста есть JSON со признаками РЕЗУЛЬТАТА инструмента.

    Требуется ключ ``success`` плюс как минимум два из ключей-подсказок
    (``size_bytes`` / ``path`` / ``content`` / ``result`` / ``output``) — этого
    достаточно для надёжного отсева нарратива tool-result, при этом обычный
    содержимый файл (где таких ключей нет) не отклоняется.
    """
    for blob in _iter_embedded_json_objects(text):
        try:
            payload = json.loads(blob)
        except ValueError:
            continue
        if not isinstance(payload, dict):
            continue
        hints = [key for key in _TOOL_RESULT_JSON_HINTS if key in payload]
        if "success" in payload and len(hints) >= 2:
            return True
    return False


def clean_telegram_tags(text: str) -> str:
    """Remove obsolete Telegram tags (WRITE_FILE, RUN_SHELL, SEND_FILE) and instructions."""
    if not text:
        return ""
    lines: list[str] = []
    for line in text.splitlines():
        if any(
            tag in line
            for tag in (
                "WRITE_FILE|",
                "RUN_SHELL|",
                "SEND_FILE|",
                "Telegram-бота",
                "SEND_FILE",
                "WRITE_FILE",
                "RUN_SHELL",
            )
        ):
            continue
        lines.append(line)
    result = "\n".join(lines)
    result = re.sub(r"\n{3,}", "\n\n", result)
    return result.strip()


def extract_request_literals(text: str) -> tuple[list[str], bool]:
    """Извлечь из запроса кандидаты-литералы содержимого (LOOP 6 / DEFECT E2E).

    Returns:
        tuple[list[str], bool]: (candidates, is_mandatory_all).
        Если найден блок маркера содержимого (is_mandatory_all=True), черновик
        обязан содержать ВСЕ кандидаты.
        Если маркера нет (is_mandatory_all=False), черновик обязан содержать
        ВСЕ найденные кандидаты-литералы.
    """
    # 1. Поиск блока содержимого после маркеров с двоеточием
    marker_match = re.search(
        r"(?i)(?:двумя\s+строками|содержим\w*(?:\s+файла)?(?:\s+двумя\s+строками)?|с\s+текстом|текстом|текст|строками)\s*:\s*(.+)",
        text,
        re.DOTALL,
    )
    if marker_match:
        block = marker_match.group(1)
        stop_match = re.search(
            r"(?i)(?:^|\n|\s)(?:(?:и|а|а\s+также|затем)\s+)?(?:Затем|Покажи|Прочитай|Прочти|Читай|Выведи|Открой)\b",
            block,
        )
        if stop_match:
            block = block[: stop_match.start()]
        lines = [line.strip() for line in block.strip().splitlines() if line.strip()]
        if lines:
            # FAILURE E (guio.md LIVE BLOCKER): inline-блок содержимого может
            # содержать НЕСКОЛЬКО литералов, разделённых «и»/«запятой»/«слэшем»
            # на одной строке («...двумя строками: ANTIGONA FILE TEST и 12345»).
            # Раньше вся строка «ANTIGONA FILE TEST и 12345.» возвращалась ОДНИМ
            # литералом, из-за чего validate_draft_literals требовал этот текст
            # дословно и отвергал корректный черновик → ложная clarification.
            # Теперь строка разбивается на отдельные литералы, а trailing
            # пунктуация (точка, запятая) удаляется.
            flat: list[str] = []
            for line in lines:
                for piece in re.split(r"\s+(?:и|,|;|/)\s+", line):
                    piece = piece.strip().rstrip(".,;:!?»\"'")
                    if piece:
                        flat.append(piece)
            return flat, True

    # 1b. Поиск inline-содержимого без двоеточия («с текстом hello», «текст Z», «с содержимым Z»)
    inline_match = re.search(
        r"(?i)\b(?:с\s+текстом|текстом|текст|с\s+содержимым|содержимым)\s+([^\n\r]+)",
        text,
    )
    if inline_match:
        tail = inline_match.group(1)
        stop_match = re.search(
            r"(?i)(?:^|\n|\s)(?:(?:и|а|а\s+также|затем)\s+)?(?:Затем|Покажи|Прочитай|Прочти|Читай|Выведи|Открой)\b",
            tail,
        )
        if stop_match:
            tail = tail[: stop_match.start()]
        cand = tail.strip()
        cand = re.sub(r"(?i)\s+(?:и|а|а\s+также)$", "", cand).strip()
        cand = cand.strip(" \t\n\r\"'.,;:«»")
        if cand:
            return [cand], True

    # 2. Если маркера нет — поиск uppercase-последовательностей и чисел \d{3,}
    text_clean = re.sub(r"\b[\w./\\-]+\.[a-zA-Z0-9]+\b", " ", text)
    upper_matches = re.findall(r"\b[A-ZА-ЯЁ]{2,}(?:\s+[A-ZА-ЯЁ]{2,})*\b", text_clean)
    valid_upper = [u.strip() for u in upper_matches if len(u.strip()) >= 4]
    digit_matches = re.findall(r"\b\d{3,}\b", text_clean)
    candidates = valid_upper + digit_matches
    if candidates:
        return candidates, False

    return [], False


def validate_draft_literals(request_text: str, draft_content: str) -> bool:
    """Проверить, что черновик содержит требуемые литералы запроса (fail-closed).

    1. Если в запросе найден блок маркера содержимого («двумя строками:»,
       «содержимое...:», inline «с текстом hello» и т.п.), черновик ОБЯЗАН
       содержать ВСЕ кандидаты этого блока.
    2. Если маркера нет, но в запросе найдены uppercase-слова (>=4 символов)
       или числовые последовательности (>=3 цифр), черновик ОБЯЗАН содержать
       ВСЕ найденные кандидаты.
    3. Если кандидатов в запросе нет («опиши себя»), генеративный черновик валиден.
    """
    candidates, _ = extract_request_literals(request_text)
    if not candidates:
        return True
    return all(cand in draft_content for cand in candidates)


class DialogueEngine:
    """Canonical dialogue engine for Antigona conversations.

    Manages conversational turns with persistence via SessionRepository,
    context assembly via ContextBuilder (including USER.md, MEMORY.md, AGENTS.md),
    prompt cleaning, and natural responses for small talk / queries without TaskFlow.
    """

    def __init__(
        self,
        repository: SessionRepository | None = None,
        provider: BaseProvider | None = None,
        context_builder: ContextBuilder | None = None,
        db_path: str | None = None,
        database: Any | None = None,
        registry: Any | None = None,
    ) -> None:
        self.repository = repository or SessionRepository(db_path=db_path)
        self.provider = provider
        self.registry = registry
        self.context_builder = context_builder or ContextBuilder()
        if self.context_builder and hasattr(self.context_builder, "persona"):
            self.context_builder.persona = clean_telegram_tags(self.context_builder.persona)
        # Единая память ядра (Step 5-6): MemoryRepository над memory_entries.
        # Если задан — подмешивается в промпт и принимает MEMORIZE-команды.
        self.memory_repository = None
        if database is not None:
            from antigona.core.memory_repository import MemoryRepository

            self.memory_repository = MemoryRepository(database)
            if self.context_builder is not None:
                self.context_builder.memory_repository = self.memory_repository

        # Truth contract (turn-local outcome): the outcome of a turn belongs to
        # THAT turn only.  These are reset at the start of every ``reply()`` so a
        # turn that executes no tool can never be attributed the outcome (or the
        # error text) of an earlier turn.
        self._last_tool_outcome: str | None = None
        self._last_tool_error: str | None = None

    async def close(self) -> None:
        """Close the underlying session repository and injected provider if open."""
        if self.repository is not None:
            await self.repository.close()
        if self.provider is not None and hasattr(self.provider, "close"):
            self.provider.close()

    async def __aenter__(self) -> DialogueEngine:
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()

    async def reply(
        self,
        text: str,
        session_id: str = "cli-session",
        context: dict[str, Any] | None = None,
    ) -> str:
        """Process a conversational turn, persisting history to SessionRepository in antigona_sessions.db.

        Returns a natural response string without creating a TaskFlow.
        """
        # Truth contract: a turn's outcome is derived ONLY from this turn's
        # execution.  Clearing here (not just inside ``_maybe_run_tool``) means a
        # turn that runs no tool reports no outcome — a prior turn's failure can
        # never be re-presented as this turn's result.
        self._last_tool_outcome = None
        self._last_tool_error = None

        stripped = text.strip()
        if not stripped:
            return "..."

        # Ensure DB connection
        if self.repository.db._conn is None:
            await self.repository.connect()

        # Ensure session exists
        if not await self.repository.session_exists(session_id):
            await self.repository.create_session(session_id=session_id, title="CLI Session")

        # Load history for session to assemble turn_buffer
        raw_msgs = await self.repository.get_messages(session_id, limit=100)
        turn_buffer: list[dict[str, Any]] = [
            {"role": m["role"], "content": m["content"]}
            for m in raw_msgs
            if m["role"] in ("user", "assistant")
        ]

        # Record current user message in DB
        await self.repository.add_message(
            session_id=session_id,
            role="user",
            content=stripped,
        )
        turn_buffer.append({"role": "user", "content": stripped})

        # Authenticated owner of the turn (gateway token / telegram identity).
        # Threaded into tool dispatch as the reserved ``_owner_id`` kwarg so
        # owner-gated tools (e.g. tmux) can hard-deny non-owners.
        turn_owner_id = str((context or {}).get("owner_id") or "")
        turn_channel = str((context or {}).get("channel") or "")
        turn_correlation_id = str((context or {}).get("correlation_id") or "")
        turn_turn_id = str((context or {}).get("turn_id") or "")
        # Bound context string lengths and sanitize control characters
        turn_channel = turn_channel[:64].replace("\n", "").replace("\r", "")
        turn_correlation_id = turn_correlation_id[:256].replace("\n", "").replace("\r", "")
        turn_turn_id = turn_turn_id[:256].replace("\n", "").replace("\r", "")

        provider: BaseProvider | None = self.provider
        if provider is None:
            from antigona.providers.resolver import ProviderResolver

            provider = ProviderResolver.get_provider()
        reply_text = ""

        if provider is not None:
            try:
                # Build message list with ContextBuilder (SOUL/AGENTS + единая
                # БД-память владельца сессии).
                memory_owner = session_id.rsplit(":", 1)[-1] if ":" in session_id else session_id
                messages = self.context_builder.build(
                    turn_buffer=turn_buffer,
                    memory_owner_id=memory_owner,
                )
                if messages and messages[0].get("role") == "system":
                    messages[0]["content"] = clean_telegram_tags(messages[0]["content"])
                    # Integration: advertise available tools to the LLM
                    tools_block = self._integration_tools_block()
                    if tools_block:
                        messages[0]["content"] = messages[0]["content"] + "\n\n" + tools_block

                reply_text = provider.generate(messages)
                # Integration: execute a tool call if the model requested one
                reply_text = await self._maybe_run_tool(
                    reply_text,
                    owner_id=turn_owner_id,
                    channel=turn_channel,
                    session_id=session_id,
                    correlation_id=turn_correlation_id,
                    turn_id=turn_turn_id,
                )
            except Exception as exc:
                logger.warning("LLM provider generation failed: %s; using fallback", exc)
                reply_text = self._fallback_reply(stripped, turn_buffer)
        else:
            reply_text = self._fallback_reply(stripped, turn_buffer)

        # ── Truthfulness: never replay a previous turn's failure ──────────
        # This turn executed no tool (no outcome was produced):
        #   * if the model merely echoed an earlier turn's recorded failure text,
        #     or produced text carrying fail-closed/security internals, do NOT
        #     present it as the current answer — answer neutrally instead.
        if self._last_tool_outcome is None and reply_text:
            if _is_stale_error_replay(
                reply_text, _prior_tool_error_texts(turn_buffer)
            ) or _contains_internal_error_marker(reply_text):
                logger.info(
                    "Suppressed a stale/echoed previous-turn failure in the "
                    "conversation reply (session=%s)", session_id,
                )
                reply_text = _NEUTRAL_NO_ACTION_REPLY

        # Clean any accidental Telegram tags from reply
        reply_text = clean_telegram_tags(reply_text)

        # Единая память ядра: обработать MEMORIZE-команды из ответа модели
        if self.memory_repository is not None and ":" in session_id:
            memory_owner = session_id.rsplit(":", 1)[-1]
            reply_text, _memorized = self._extract_and_store_memorize(reply_text, memory_owner)

        # Record assistant reply in DB
        await self.repository.add_message(
            session_id=session_id,
            role="assistant",
            content=reply_text,
        )

        return reply_text

    async def draft_file_content(
        self, text: str, session_id: str = "cli-session"
    ) -> str | None:
        """Черновик содержимого файла или ``None``.

        Тонкая обёртка над :meth:`draft_file_content_result` для вызывающих
        сторон, которым не нужен статус.
        """
        return (await self.draft_file_content_result(text, session_id)).content

    async def draft_file_content_result(
        self, text: str, session_id: str = "cli-session"
    ) -> FileContentDraft:
        """Generate the literal content a ``workspace.write_text`` task should write.

        Unlike :meth:`reply`, this is not a conversational turn: it is a
        one-shot generation call whose entire output becomes the file body.
        Recent session history is included so requests that refer back to
        earlier context (``"сделай из этого текста документ"``) resolve
        correctly instead of writing the literal instruction sentence to disk.

        Статус результата (LOOP3, DEFECT 3) отделяет degraded-режим от
        настоящего отказа:

        * ``DRAFT_OK`` — содержимое получено;
        * ``DRAFT_UNAVAILABLE`` — провайдера нет или он недоступен
          (degraded/offline): вызывающая сторона вправе применить свой
          fallback;
        * ``DRAFT_REJECTED`` — провайдер ОТВЕТИЛ, но вывод непригоден как
          тело файла (пусто либо это вопрос-уточнение). Записывать вместо
          него инструкцию владельца НЕЛЬЗЯ — нужно спросить владельца.

        Провайдер резолвится тем же каноническим путём, что и :meth:`reply`
        (:class:`ProviderResolver`), чтобы чат и черновик файла никогда не
        расходились в том, какой провайдер активен.
        """
        stripped = text.strip()
        if not stripped:
            return FileContentDraft(None, DRAFT_UNAVAILABLE)

        provider = self.provider or self._resolve_provider()
        if provider is None:
            logger.warning("draft_file_content: no LLM provider resolved")
            return FileContentDraft(None, DRAFT_UNAVAILABLE)

        history: list[dict[str, Any]] = []
        try:
            if self.repository.db._conn is None:
                await self.repository.connect()
            raw_msgs = await self.repository.get_messages(session_id, limit=20)
            history = [
                {"role": m["role"], "content": m["content"]}
                for m in raw_msgs
                if m["role"] in ("user", "assistant")
            ][-10:]
        except Exception as exc:
            logger.warning("draft_file_content: history load failed: %s", exc)
            history = []

        system_prompt = (
            "Ты — модуль генерации содержимого файла для агента Antigona. "
            "Пользователь попросил записать файл. Разбери запрос и, если "
            "нужно, историю диалога (например, запрос может ссылаться на "
            "«этот текст» из более раннего сообщения) и выведи РОВНО то "
            "содержимое, которое должно оказаться в файле — без приветствий, "
            "пояснений, кавычек-обёрток или пересказа задачи. Если запрос "
            "описывает, что должно быть в файле (например, «опиши себя»), "
            "напиши сам текст, а не инструкцию по его написанию."
        )
        messages = [
            {"role": "system", "content": system_prompt},
            *history,
            {"role": "user", "content": stripped},
        ]

        try:
            content = provider.generate(
                messages, context={"max_tokens": DRAFT_MAX_TOKENS}
            )
        except Exception as exc:
            # Провайдер недоступен (сеть/HTTP/формат) — неотличимо от offline,
            # поэтому degraded, а не отказ.
            logger.warning("draft_file_content: provider.generate failed: %s", exc)
            return FileContentDraft(None, DRAFT_UNAVAILABLE)

        content = clean_telegram_tags((content or "").strip())
        if not content:
            # Провайдер ответил HTTP 200 с пустым message.content —
            # наблюдаемый режим отказа deepseek-v4-flash (LOOP3, ШАГ 1).
            logger.warning("draft_file_content: provider returned empty content")
            return FileContentDraft(None, DRAFT_REJECTED)
        if looks_like_tool_protocol_markup(content):
            logger.warning(
                "draft_file_content: model returned tool protocol markup"
            )
            return FileContentDraft(None, DRAFT_REJECTED)
        if looks_like_clarification(content):
            logger.warning(
                "draft_file_content: model asked for clarification instead of content"
            )
            return FileContentDraft(None, DRAFT_REJECTED)
        if not validate_draft_literals(stripped, content):
            logger.warning("draft_file_content: draft missing request literals")
            return FileContentDraft(None, DRAFT_REJECTED)
        return FileContentDraft(content, DRAFT_OK)

    def _resolve_provider(self) -> BaseProvider | None:
        """Канонический резолв провайдера — тот же путь, что у :meth:`reply`."""
        from antigona.providers.resolver import ProviderResolver

        return ProviderResolver.get_provider()

    _INTEGRATION_TOOLS = {
        "kanban": "действия: create (title), move (card_id, column), done (card_id), get (card_id), list",
        "mcp": "действия: list, add (command/url), remove (server)",
        "acp": "действия: list, add (base_url), remove (agent)",
        "rss": "забрать новости: url, limit",
        "loop": "цикл с проверкой: max_iterations",
        "count_tokens": "посчитать токены: text",
        "send_file": "отправить файл в Telegram: path (абсолютный путь), caption (подпись, опционально)",
        "generate_image": "сгенерировать изображение по описанию (Pollinations.ai, бесплатно, без ключа): prompt (описание картинки, англ. лучше)",
        "write_file": "создать/записать файл на диск: path (абсолютный путь), content (содержимое)",
        "read_file": "прочитать реальное содержимое файла с диска: path (абсолютный путь)",
        "tmux": "тихие фоновые сессии (только владелец): start (command), send (keys), read (session, lines), list, kill (session), status (session)",
        "frontend_build": "собрать/проверить frontend: project (allowlisted root, default frontend_ts), mode (check|build|check_and_build), install_dependencies (bool), clean_build (bool)",
        "tts": "озвучить текст в аудио/голосовое сообщение (speech.tts): text, voice (alloy)",
        "speech_tts": "озвучить текст (speech.tts): text, voice (alloy)",
        "archive_inspect": "проверить содержимое архива без распаковки: path",
        "archive_extract": "безопасно распаковать архив: path, target_dir",
        "archive_create": "создать ZIP/TAR архив: output_path, source, format",
        "system_time": "получить текущее системное время и дату",
        "create_document": "создать и проверить документ (txt, md, json, csv): filename, content, doc_type",
    }

    def _integration_tools_block(self) -> str:
        """Advertise integration tools the model may call."""
        if not self.registry:
            return ""
        lines = ["--- ДОСТУПНЫЕ ИНСТРУМЕНТЫ (интеграции) ---"]
        for name, desc in self._INTEGRATION_TOOLS.items():
            lines.append("- " + name + ": " + desc)
        lines.append("Вызвать инструмент можно так: " + "⟪" + "tool:kanban action=\"create\" title=\"Задача\"" + "⟫" + " и я выполню его.")
        return "\n".join(lines)

    async def _maybe_run_tool(
        self,
        reply_text: str,
        owner_id: str = "",
        channel: str = "",
        session_id: str = "",
        correlation_id: str = "",
        turn_id: str = "",
    ) -> str:
        """If the model emitted a tool call, dispatch it safely through UnifiedToolExecutionLayer."""
        # Truth contract: expose the REAL tool outcome of this call so the
        # caller (brain) can record it; reset first so a stale value from a
        # previous turn can never be attributed to this one.
        self._last_tool_outcome = None
        self._last_tool_error = None
        session_id = session_id[:256] if session_id else ""
        if not self.registry:
            return reply_text
        import re

        from antigona.engine.unified_executor import ToolExecutionRequest, UnifiedToolExecutionLayer

        # pattern: ⟪tool:NAME key="val" ...⟫
        pat = chr(0x27ea) + "tool:([a-zA-Z_.]+)" + "((?:\\s+[a-zA-Z_]+=\"[^\"]*\")*)\\s*" + chr(0x27eb)
        m = re.search(pat, reply_text)
        if not m:
            return reply_text
        name = m.group(1)

        # Mapping aliases to canonical names
        canonical_map = {
            "tts": "speech.tts",
            "speech_tts": "speech.tts",
            "text_to_speech": "speech.tts",
            "sandbox_shell": "sandbox.shell",
            "sandbox-shell": "sandbox.shell",
            "archive_inspect": "archive.inspect",
            "archive_extract": "archive.extract",
            "archive_create": "archive.create",
            "system_time": "system.time",
            "create_document": "workspace.create_document",
        }
        dispatch_name = canonical_map.get(name, name)
        # Canonical contract tools are directly callable even though they are
        # not "integration" tools; safe aliases resolve to the SAME tool (never
        # a more privileged one), so a legitimate intent is not denied on a
        # mere name mismatch.
        canonical_contracts = {
            "speech.tts",
            "archive.inspect",
            "archive.extract",
            "archive.create",
            "system.time",
            "workspace.create_document",
            "sandbox.shell",
        }

        # Allow execution ONLY for advertised integration tools or canonical contract tools
        if (
            name not in self._INTEGRATION_TOOLS
            and dispatch_name not in self._INTEGRATION_TOOLS
            and dispatch_name not in canonical_contracts
        ):
            logger.warning("Denied unadvertised tool invocation attempt: '%s'", name)
            self._last_tool_outcome = "DENIED"
            self._last_tool_error = f"tool {name!r} is not available in this mode"
            reply_text = reply_text[: m.start()] + reply_text[m.end():]
            return reply_text.strip() + f"\n\n⚠️ Инструмент `{name}` недоступен в этом режиме или требует вызова через систему политик."

        args = {}
        for am in re.finditer(r"([a-zA-Z_]+)=\"([^\"]*)\"", m.group(2)):
            args[am.group(1)] = am.group(2)

        args.pop("_owner_id", None)
        if name == "tmux":
            args["_owner_id"] = owner_id

        unified = getattr(self, "_unified_executor", None)
        if not unified:
            unified = UnifiedToolExecutionLayer(registry=self.registry)
            self._unified_executor = unified

        # Use real context from the originating request; fall back to
        # safe defaults only when the caller genuinely omitted values.
        req = ToolExecutionRequest(
            tool_name=dispatch_name,
            params=args,
            requester="llm",
            channel=channel or "cli",
            user_id=owner_id or "owner",
            session_id=session_id or "dialogue-session",
            correlation_id=correlation_id or f"llm-tool-{name}",
            turn_id=turn_id or correlation_id or f"llm-tool-{name}",
        )

        try:
            result = await unified.execute(req)
            result_text = str(result)
            outcome, error = _tool_result_outcome(result_text)
            self._last_tool_outcome = outcome
            self._last_tool_error = error
            reply_text = reply_text[: m.start()] + reply_text[m.end():]
            voice_marker = voice_marker_from_tool_result(result_text)
            if voice_marker:
                # Deliver the real audio instead of dumping escaped JSON: the
                # channel turns the literal marker into a voice message and
                # strips it from the visible text.
                return (
                    reply_text.strip()
                    + "\n\n🎙 Озвучил текст — голосовое сообщение ниже.\n"
                    + voice_marker
                )
            return reply_text.strip() + "\n\nИнструмент `" + name + "` выполнен: " + result_text
        except Exception as exc:
            self._last_tool_outcome = "FAILED"
            self._last_tool_error = str(exc)
            return reply_text.strip() + "\n\nИнструмент `" + name + "` не выполнен: " + str(exc)

    def _extract_and_store_memorize(

        self, reply_text: str, owner_id: str
    ) -> tuple[str, int]:
        """Extract MEMORIZE|kind|content commands and store them in DB-memory.

        Returns (cleaned_reply, stored_count). The commands are removed from
        the user-visible reply; facts land in memory_entries (Step 5-6).
        """
        import re as _re

        pattern = _re.compile(
            r"MEMORIZE\|(user|memory|preference|profile)\|([^\n]+)", _re.IGNORECASE
        )
        stored = 0
        cleaned = reply_text

        def _store(match: _re.Match[str]) -> str:
            nonlocal stored
            kind = match.group(1).lower()
            content = match.group(2).strip()
            if content and self.memory_repository is not None:
                try:
                    self.memory_repository.remember(
                        owner_id,
                        content,
                        kind=kind,
                        title=content[:80],
                        source="core",
                    )
                    stored += 1
                except Exception as exc:
                    logger.warning("MEMORIZE store failed: %s", exc)
            return ""

        cleaned = pattern.sub(_store, cleaned)
        return cleaned.strip(), stored

    def _fallback_reply(self, text: str, turn_buffer: list[dict[str, Any]]) -> str:
        """Deterministic, natural response fallback when no LLM provider is active or call fails."""
        low = text.lower().strip()

        # Questions about identity of agent ("Кто ты?")
        if any(
            phrase in low
            for phrase in ("кто ты", "кто ты такая", "какая у тебя роль", "кто тебя создал", "who are you")
        ):
            return f"Я — Antigona, ваш интеллектуальный разговорный агент. Мой создатель — {owner_name()}. Я помогаю в решении задач, анализе и управлении системой."

        # Questions about project structure ("Как устроен этот проект?")
        if any(
            phrase in low
            for phrase in (
                "как устроен этот проект",
                "как устроен проект",
                "архитектура проекта",
                "архитектура antigona",
                "что ты умеешь",
                "как работаешь",
            )
        ):
            return (
                "Antigona построена по канонической серверной архитектуре:\n"
                "1. Единый Gateway REST API (/api/v1/dialogue/turn) объединяет CLI и Telegram.\n"
                "2. DialogueEngine ведет персистентную историю сессий в SQLite и учитывает USER.md и MEMORY.md.\n"
                "3. OwnerOverrideManager управляет правами доступа с 2-ступенчатым подтверждением CRITICAL-действий.\n"
                "4. ActionExecutor и Verifier гарантируют проверенный результат выполнения системных команд."
            )

        # Questions about identity ("Кто я?")
        if any(
            phrase in low
            for phrase in ("кто я", "как меня зовут", "кто я такой", "кто я такая", "who am i")
        ):
            user_profile = ""
            if self.context_builder and getattr(self.context_builder, "_frozen_memory", None):
                user_profile = self.context_builder._frozen_memory.get("user", "").strip()
            owner = owner_name()
            if not user_profile or owner.lower() in user_profile.lower():
                return f"Вы — {owner}, создатель и главный разработчик Antigona."
            return f"Судя по профилю пользователя, вы:\n{user_profile}"

        # Questions about conversation history ("О чём мы говорили?")
        if any(
            phrase in low
            for phrase in (
                "о чём мы говорили",
                "о чем мы говорили",
                "что мы обсуждали",
                "о чем был разговор",
                "о чём был разговор",
                "история диалога",
            )
        ):
            past_turns = [t for t in turn_buffer[:-1] if t.get("content")]
            if not past_turns:
                return "Мы только начали наш диалог. Ранее сообщений не было."
            recent = past_turns[-6:]
            summaries = []
            for t in recent:
                role_label = owner_name() if t.get("role") == "user" else "Antigona"
                snippet = str(t.get("content", "")).replace("\n", " ")
                if len(snippet) > 80:
                    snippet = snippet[:77] + "..."
                summaries.append(f"• {role_label}: {snippet}")
            return "О чём мы говорили в недавних сообщениях:\n" + "\n".join(summaries)

        # FAILURE C/D (MASTER LOOP v2.1, guio.md): история разговора может
        # использоваться для рассуждения, но НЕ должна становиться финальным
        # ответом, а оригинальный user-запрос НИКОГДА не возвращается как
        # «факт» из памяти. Раньше здесь был context-retention fallback,
        # возвращавший `f"Судя по нашему разговору: {content}"` (echo оригинала)
        # при недоступном LLM — прямое нарушение FAILURE C (never echo the
        # original request) и FAILURE D (history must never become the answer).
        # Удалено: fallback обязан быть честным generic-ответом, не эхом.

        # Confirmation / Affirmation ("Да")
        if low in ("да", "давай", "согласен", "согласна", "ок", "ok", "yes", "хорошо"):
            return "Отлично! Чем ещё могу помочь?"

        # Negation ("Нет")
        if low in ("нет", "не надо", "no", "отмена", "стоп"):
            return "Поняла. Если появятся другие задачи — обращайтесь!"

        # Continuation ("Продолжай")
        if low in ("продолжай", "продолжить", "продолжай работу", "дальше", "continue"):
            return "Слушаю вас! Что сделаем дальше?"

        # Greetings
        if any(
            w in low
            for w in ("привет", "здравствуй", "здравствуйте", "хай", "hello", "hi", "добрый")
        ):
            return "Привет! Я Antigona. Готова помочь с любыми задачами."

        return (
            "Я Antigona. Свободный диалог готов. "
            "Опишите задачу или вопрос, и я помогу вам."
        )
