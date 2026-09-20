"""AntigonaBrain — единое серверное ядро Antigona.

Единственная точка входа для обработки пользовательского ввода в каноническом
серверном runtime (Gateway). CLI и Telegram — тонкие клиенты: они НЕ создают
локальные DialogueEngine/IntentRouter и НЕ выполняют задачи сами — они вызывают
единый Gateway/Turn API, который обрабатывает ввод через ``AntigonaBrain.process()``.

Архитектура (5 слоёв из канонической модели):
    1. Conversation Layer  — понимание + контекст (DialogueEngine)
    2. Session & Memory     — единая память (SessionRepository)
    3. Intent & Policy      — классификация + права (IntentRouter)
    4. Execution Layer      — каноническое выполнение (task_backend)
    5. Gateway Layer        — реальное исполнение (серверный Gateway)

Usage (серверный runtime, внутри Gateway)::

    brain = AntigonaBrain(
        dialogue_engine=dialogue_engine,
        session_repository=session_repo,
        task_backend=gateway_task_backend,
    )
    await brain.connect()
    response = await brain.process(
        text="Создай файл test.txt",
        user_id="OWNER_CHAT_ID",
        channel="telegram",
    )
    print(response.text)
    await brain.close()
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from inspect import isawaitable
from pathlib import Path
from typing import Any, Protocol

from antigona.conversation.dialogue_engine import DialogueEngine
from antigona.filesystem import WorkspaceViolation, validate_relative_path
from antigona.memory.summarizer import MemorySummarizer
from antigona.router.intent_router import IntentDecision, IntentRouter
from antigona.sessions.repository import SessionRepository
from antigona.task_goal import (
    parse_goal,
    requires_exact_write_read_contract,
    resolve_free_text_request,
    strip_code_fences,
)
from antigona.tools.workspace_read import TOOL_NAME as WORKSPACE_READ_TEXT

logger = logging.getLogger(__name__)

# ── Shell-команда из свободного текста (task.shell) ─────────────────────────
# Русские глаголы-обёртки, после которых ожидается ascii-команда.
_SHELL_WRAPPER_VERBS = frozenset(
    {
        "выполни", "выполните", "выполнить",
        "запусти", "запустите", "запустить",
        "прогони", "прогнать",
        "покажи", "покажите", "показать",
        "проверь", "проверьте", "проверить",
        "найди", "найдите", "найти",
        "выведи", "выведите", "вывести",
        "открой", "откройте", "открыть",
        "прочитай", "прочитайте", "прочитать",
    }
)

#: Explicit shell-invocation prefix, e.g. ``shell: uname -a`` or ``bash: ls``.
#: The prefix is an addressing convention, NOT part of the command — leaving it
#: in place makes the sandbox receive a literal ``shell:`` argument and fail.
_SHELL_PREFIX_RE = re.compile(
    r"^\s*(?:shell|bash|sh|zsh|терминал|консоль|командная\s+строка)"
    r"\s*[:：]\s*",
    re.IGNORECASE,
)
#: Russian "выполни команду <cmd>" prefix (verb + explicit noun).  The generic
#: verb-only form is already handled by ``_SHELL_WRAPPER_VERBS``.
_SHELL_COMMAND_VERB_RE = re.compile(
    r"^\s*(?:выполни|выполните|выполнить|запусти|запустите|запустить|"
    r"прогони|прогоните|прогнать|исполни|исполните|исполнить)"
    r"\s+(?:команду|команда|команды|эту\s+команду)\s*[:：]?\s*",
    re.IGNORECASE,
)


def _strip_shell_prefix(text: str) -> str:
    """Remove an addressing prefix (``shell:`` / ``выполни команду``) from *text*.

    Idempotent and tolerant of extra whitespace and of the two colon glyphs.
    Never invents a command: it only removes the wrapper the owner typed.
    """
    stripped = text
    for pattern in (_SHELL_PREFIX_RE, _SHELL_COMMAND_VERB_RE):
        stripped = pattern.sub("", stripped, count=1)
    return stripped.strip()
_CYRILLIC_RE = None

_BARE_SHELL_COMMANDS = frozenset(
    {
        "pwd",
        "ls",
        "ll",
        "whoami",
        "id",
        "uname",
        "date",
        "uptime",
        "hostname",
        "ps",
        "df",
        "free",
        "who",
        "w",
    }
)


def _is_bare_shell_command(text: str) -> bool:
    """Check if the text is a bare deterministic shell command (e.g. pwd, ls)."""
    global _CYRILLIC_RE
    if _CYRILLIC_RE is None:
        import re as _re

        _CYRILLIC_RE = _re.compile(r"[а-яА-ЯёЁ]")

    t = (text or "").strip().strip("\"'`").rstrip(".;,!")
    if not t or _CYRILLIC_RE.search(t):
        return False
    words = t.split()
    return bool(words and words[0].lower() in _BARE_SHELL_COMMANDS)


_INSTALL_NL_RE_PATTERN = r"^(?:install|установи|установить|поставь|postav)\s+(\S.*)$"

_URL_RE_PATTERN = r"https?://[^\s<>\"']+"


#: Substrings that mark a fail-closed SECURITY denial (ownership/fence/policy)
#: rather than an ordinary execution error.  Used to classify a tool outcome so
#: a denial is never silently presented as an ordinary success.
_DENIAL_MARKERS: tuple[str, ...] = (
    "fencing token",
    "ownership fence",
    "denied_stale_fence",
    "fenced",
    "fence",
    "ownership",
    "policy_denied",
    "policy denied",
    "denied by policy",
    "denied",
    "access denied",
    "not permitted",
    "refused",
    "запрещен",
    "отклонен",
)


def _classify_tool_outcome(error_reason: str | None) -> str:
    """Classify a real tool error into a truth-contract outcome token.

    ``DENIED`` when the error is a fail-closed security denial (ownership /
    workspace fence / policy), otherwise ``FAILED``.  Never returns SUCCEEDED —
    callers only invoke this on an actual error.
    """
    if not error_reason:
        return "FAILED"
    low = error_reason.lower()
    if any(marker in low for marker in _DENIAL_MARKERS):
        return "DENIED"
    return "FAILED"


def _extract_urls(text: str) -> list[str]:
    """Return http(s) URLs found in *text* (deduplicated, order preserved)."""
    if not text:
        return []
    import re as _re

    seen: list[str] = []
    for u in _re.findall(_URL_RE_PATTERN, text):
        if u not in seen:
            seen.append(u)
    return seen


def _is_bare_url(text: str) -> bool:
    """True if the whole message is just one URL (nothing else worth routing)."""
    t = (text or "").strip()
    if not t:
        return False
    urls = _extract_urls(t)
    if len(urls) != 1:
        return False
    # Strip surrounding punctuation; if what remains is only the URL → bare.
    stripped = t.strip(" \t\n\"'`.;,!()[]")
    return stripped == urls[0]


def _fetch_web_content(url: str) -> str:
    """Fetch readable content from *url* for LLM context; '' on failure."""
    try:
        from antigona.tools.web_search import WebSearchTool

        res = WebSearchTool().extract(url)
        if res.success and res.content:
            return f"[Содержимое {url}]:\n{res.content}\n"
    except Exception:
        pass
    return ""


def _translate_install_command(t: str) -> str | None:
    """Map a natural-language install request to a real pip command.

    ``Install edge-tts`` / ``установи uv`` are not valid shell commands — there
    is no ``install`` binary. In the install-capable Python sandbox the natural
    action is ``pip install <pkg>``. We fold these to the real command. Already
    real commands (``apt install X``, ``pip install X``, ``uv add X``) start
    with a tool name, not the bare verb ``install``, so they are left alone.
    """
    if not t:
        return None
    import re as _re

    m = _re.match(_INSTALL_NL_RE_PATTERN, t.strip(), _re.IGNORECASE)
    if not m:
        return None
    target = m.group(1).strip().strip("\"'`")
    if not target:
        return None
    return f"pip install {target}"


def _extract_shell_command(text: str) -> str | None:
    """Извлечь shell-команду из свободного запроса (эвристика).

    Правила:
    - чистый ascii-текст (без кириллицы) — команда как есть (``ls -a``);
    - русская обёртка + ascii-хвост — хвост как команда (``Выполни pwd`` → ``pwd``);
    - NL-установка (``Install X`` / ``установи X``) → ``pip install X``;
    - иначе None (не shell-команда — остаётся дефолтный write-путь).
    """
    global _CYRILLIC_RE
    if _CYRILLIC_RE is None:
        import re as _re

        _CYRILLIC_RE = _re.compile(r"[а-яА-ЯёЁ]")

    t = (text or "").strip().strip("\"'`").rstrip(".;,!")
    if not t:
        return None
    t = _strip_shell_prefix(t)
    if not t:
        return None
    # A bare URL is not a shell command — let it route to the conversation
    # handler, which fetches the linked content for the LLM.
    if _is_bare_url(t):
        return None
    translated = _translate_install_command(t)
    if translated is not None:
        return translated
    if not _CYRILLIC_RE.search(t):
        return t
    words = t.split(maxsplit=1)
    if len(words) == 2 and words[0].lower() in _SHELL_WRAPPER_VERBS:
        tail = words[1].strip().strip("\"'`")
        if tail and not _CYRILLIC_RE.search(tail):
            return tail
    return None


def _has_read_back_intent(text: str) -> bool:
    """Детерминированно определить, что запрос после создания файла просит прочитать его обратно (LOOP 6).

    Ищет токены «прочитай», «прочти», «читай», «read», «read back» или фразы вида
    «покажи мне содержимое/файл/что внутри», «покажи содержимое», «выведи содержимое»,
    «покажи что внутри», «открой и покажи».
    """
    patterns = [
        r"\b(?:прочитай|прочти|читай|read|read\s+back)\b",
        r"\b(?:покажи|выведи|открой\s+и\s+покажи)\s+(?:мне\s+)?(?:содержимое|файл|что\s+внутри)\b",
        r"\bоткрой\s+и\s+покажи\b",
        r"\bпокажи\s+содержимое\b",
        r"\bвыведи\s+содержимое\b",
        r"\bпокажи\s+что\s+внутри\b",
    ]
    return any(re.search(p, text, re.IGNORECASE) for p in patterns)



_MCP_SERVER_RE = re.compile(r"\bserver\s*=\s*[\"']?([\w.-]+)", re.IGNORECASE)
_MCP_TOOL_RE = re.compile(r"\btool\s*=\s*[\"']?([\w.]+)", re.IGNORECASE)
_MCP_ARGUMENTS_JSON_RE = re.compile(r"\barguments\s*=\s*(\{.*\})", re.IGNORECASE | re.DOTALL)
_MCP_TEXT_ARG_RE = re.compile(r"\btext\s*=\s*[\"'](.*?)[\"']", re.IGNORECASE | re.DOTALL)
_TTS_VERB_RE = re.compile(
    r"\b(?:озвучь|озвучить|озвучи|озвуч|озвучка|озвучивание|"
    r"произнеси|произнести|скажи\s+вслух|прочитай\s+вслух|tts|speak)\b",
    re.IGNORECASE,
)
_TTS_TAIL_RE = re.compile(
    r"\b(?:озвучь|озвучи|озвуч|произнеси|прочитай\s+вслух|скажи\s+вслух)\b"
    r"\s*(?:файлом\s*)?[:,\-—]?\s*(.+)",
    re.IGNORECASE | re.DOTALL,
)

#: A word/phrase that REFERS to earlier assistant output instead of naming the
#: literal text to speak ("озвучь рассказ" after the assistant wrote a poem).
_TTS_REFERENCE_RE = re.compile(
    r"^\s*(?:"
    r"это|этот\s+текст|его|её|ее|их|"
    r"текст|текстик|текст\s+сообщени\w*|сообщени\w*|"
    r"рассказ\w*|стих\w*|поэм\w*|сказк\w*|истори\w*|"
    r"то\s*,?\s*что\s+ты\s+(?:написал\w*|составил\w*|придумал\w*|озвуч\w*|сказал\w*)|"
    r"предыдущ\w*\s+(?:текст|ответ|сообщени\w*)|"
    r"своё\s+сообщени\w*|свое\s+сообщени\w*|свой\s+текст|"
    r"то\s+же\s+самое|"
    r"текст\s+того\s*,?\s*что\s+ты\s+озвуч\w*"
    r")\s*$",
    re.IGNORECASE,
)

#: Explicit literal: a quoted string anywhere after the TTS verb.
_TTS_QUOTED_RE = re.compile(r"[«"'"“](?P<text>.+?)[»"'"”]", re.DOTALL)
#: Explicit literal: everything after the verb and a colon.
_TTS_COLON_RE = re.compile(
    r"\b(?:озвучь|озвучить|произнеси|прочитай\s+вслух|скажи\s+вслух)\b"
    r"\s*[:：]\s*(?P<text>.+)",
    re.IGNORECASE | re.DOTALL,
)
#: A workspace file named in the request.
_TTS_FILE_RE = re.compile(
    r"(?P<name>[\w./-]+\.(?:txt|md|markdown|json|rst|log|csv))\b",
    re.IGNORECASE,
)
#: A residual fragment left after a voice verb inside a DIRECTIVE sentence -
#: «...и озвучь его голосом.».  The words there REFER to a previous text; they
#: are never the literal payload.  Speaking them aloud was the reported live
#: defect, so such a tail must resolve like a reference, never as literal text.
_TTS_VOICE_NOUN_TAIL_RE = re.compile(
    r"^\s*(?:(?:его|её|ее|их|это|этот|эту|свой|своё|свое|мой|моё|мою)\s+)?"
    r"(?:голосом|голос|вслух|своим\s+голосом|моим\s+голосом)\s*[.!?...]*\s*$",
    re.IGNORECASE,
)
#: "show me / write out the text you voiced" retrieval request.
_RETRIEVE_VOICED_RE = re.compile(
    r"(?:текст|слова|сообщени\w*)[^\n]{0,80}"
    r"(?:озвуч\w*|произнёс\w*|произнес\w*|сказал\w*\s+вслух|читал\w*\s+вслух)",
    re.IGNORECASE,
)
#: Standalone "покажи/выведи/напиши ... текст" retrieval request.
_RETRIEVE_SHOW_RE = re.compile(
    r"^\s*(?:покажи|выведи|напиши|продиктуй|повтори|напомни)\s+(?:мне\s+)?"
    r"(?:тот\s+|этот\s+|последний\s+|своё\s+|свое\s+)?текст\b",
    re.IGNORECASE,
)

# Meta / negation guard. A message that merely TALKS ABOUT voice — a complaint,
# a question about the capability, a report that a previous attempt failed, a
# negation — is NOT an execution request and must never create a task flow or a
# phantom MCP call. Only a genuine imperative/request survives this guard.
_TTS_META_GUARD_RE = re.compile(
    r"(?:"
    r"\bне\s+(?:работает|сработал\w*|смог\w*|можешь|может|удал\w*|стал\w*|"
    r"получил\w*|выполн\w*|озвуч\w*)\b"
    r"|\b(?:почему|зачем|отчего)\b"
    r"|\bне\s+нужно\b|\bне\s+надо\b"
    r"|\b(?:умеешь|можешь|поддерживаешь|есть)\s+ли\b"
    r"|\bчто\s+такое\b"
    r"|\bкак\b[^\n]{0,20}\b(?:озвуч\w*|tts|voice|speak)\b"
    r"|\b(?:какие|какой|какова|какую|каков)\b[^\n]{0,25}\b(?:озвуч\w*|tts|voice|speak)\b"
    r"|\b(?:расскажи|объясни|поясни|подскажи|напомни)\b\s+(?:мне\s+)?"
    r"(?:про|о|об|что\s+такое)\b[^\n]{0,20}\b(?:озвуч\w*|tts|voice|speak)\b"
    r"|\bжалоб\w*\b"
    r")",
    re.IGNORECASE,
)

#: The single working TTS contract tool. There is NO edge-tts MCP server in this
#: deployment — advertising one would name a capability that cannot execute
#: (phantom capability). A human TTS request therefore resolves to the
#: already-working ``speech.tts`` contract tool, never to an MCP call.
TTS_CONTRACT_TOOL = "speech.tts"


def _tts_text(text: str) -> str:
    """Spoken text for a human TTS request (tail after the verb, else whole)."""
    tail_match = _TTS_TAIL_RE.search(text)
    tail = tail_match.group(1).strip() if tail_match else ""
    if not tail:
        pre = re.split(
            r"\bи\s+озвуч\w*\b",
            text,
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0]
        tail = re.sub(
            r"^\s*(?:напиши|составь|придумай|расскажи|сделай|напиши\s+текст)\s*",
            "",
            pre,
            flags=re.IGNORECASE,
        ).strip(" .,:;")
    return tail or text.strip()


# ── Combined "compose AND voice it" requests ─────────────────────────────────
#: A clause that asks to speak something, joined to the previous clause by «и»
#: or a separator ("... и озвучь его голосом", "... , озвучь её").
_COMBINED_VOICE_CLAUSE_RE = re.compile(
    r"(?:[,;:—–-]\s*|\bи\s+)(?:озвуч\w*|произнес\w*|скажи\s+вслух|прочитай\s+вслух)\b",
    re.IGNORECASE,
)
#: Verbs that ask the assistant to COMPOSE fresh text.
_COMPOSE_VERB_RE = re.compile(
    r"\b(?:напиши|напишите|составь|составьте|придумай|придумайте|сочини|сочините|"
    r"сгенерируй|сгенерируйте|расскажи|расскажите|подготовь|подготовьте|сделай)\b",
    re.IGNORECASE,
)


def _combined_voice_clause(text: str) -> re.Match[str] | None:
    """The voice clause of a message, if it contains one."""
    return _COMBINED_VOICE_CLAUSE_RE.search(text or "")


def _is_combined_compose_voice(text: str) -> bool:
    """True when ONE message both asks for content AND to voice that content.

    Ordering contract (live defect): the generation step must run FIRST and the
    TTS step must consume exactly its produced text.  The residual tail after a
    voice verb («... и озвучь его голосом») is never the payload.
    """
    message = text or ""
    clause = _combined_voice_clause(message)
    if clause is None:
        return False
    return bool(_COMPOSE_VERB_RE.search(message[: clause.start()]))


def _compose_instruction(text: str) -> str:
    """The composition clause of a combined request (everything before voicing)."""
    clause = _combined_voice_clause(text or "")
    head = (text or "")[: clause.start()] if clause else (text or "")
    return head.strip(" .,;:—-")


_EMAIL_ADDRESS_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")

#: An actual MCP INVOCATION (a call verb + "mcp", or "mcp ... server=/tool=").
#: A bare topical mention ("MCP-сервер", "что умеет mcp") is NOT an invocation.
_MCP_INVOCATION_RE = re.compile(
    r"(?:\b(?:вызови|вызвать|call|через|используй|use)\b[^\n]{0,20}\bmcp\b)"
    r"|(?:\bmcp\b[^\n]{0,30}\b(?:server|tool)\s*=)",
    re.IGNORECASE,
)


def _mcp_arguments(text: str) -> dict[str, Any]:
    """Extract explicit MCP-style arguments (``arguments={...}`` / ``text="..."``)."""
    arguments: dict[str, Any] = {}
    args_match = _MCP_ARGUMENTS_JSON_RE.search(text)
    if args_match:
        try:
            parsed = json.loads(args_match.group(1))
            if isinstance(parsed, dict):
                arguments = parsed
        except Exception:
            arguments = {}
    if not arguments:
        text_match = _MCP_TEXT_ARG_RE.search(text)
        if text_match:
            arguments = {"text": text_match.group(1)}
    return arguments


def _known_mcp_servers() -> list[str]:
    """Names of MCP servers currently registered (best-effort, never raises)."""
    try:
        from antigona.core.mcp import MCPRegistry

        return sorted(MCPRegistry.load().names())
    except Exception:
        return []


def _validate_mcp_server(server: str) -> str | None:
    """Fail-fast registration check before a durable flow is created.

    Returns an actionable rejection listing the known servers, or ``None`` when
    the server is registered. Never creates a doomed flow for a phantom server.
    """
    server = (server or "").strip()
    known = _known_mcp_servers()
    if server and server in known:
        return None
    tail = ", ".join(known) if known else "нет зарегистрированных MCP-серверов"
    return (
        f"⚠️ MCP-сервер «{server or '?'}» не зарегистрирован — задача НЕ создана "
        f"(fail-fast). Доступные серверы: {tail}. Зарегистрируйте сервер "
        f"(MCP add) или озвучьте текст через встроенный {TTS_CONTRACT_TOOL}."
    )


def _parse_mcp_request(text: str) -> dict[str, Any] | None:
    """Извлечь запрос на озвучку (TTS) или явный MCP-вызов из свободного текста.

    Возвращает:
    * ``{"kind": "mcp", "server", "tool", "arguments"}`` — ТОЛЬКО для явного
      формата ``mcp server=... tool=...`` (или явной пары ``server=``/``tool=``);
      вызывающий обязан проверить регистрацию сервера до создания задачи;
    * ``{"kind": "tts", "server": "", "tool": "speech.tts", "arguments"}`` —
      человеческий imperative-запрос «озвучь <текст>»; резолвится в рабочий
      контрактный инструмент ``speech.tts`` (никакого edge-tts MCP-сервера в
      этом деплое нет, поэтому рекламировать его нельзя);
    * ``None`` — это не запрос на озвучку и не MCP-вызов (обычный путь).

    Meta/negation guard: текст, который лишь УПОМИНАЕТ TTS-глагол (жалоба,
    вопрос о возможности, «не работает», «почему не озвучил»), НЕ становится
    запросом на исполнение.
    """
    if not text:
        return None

    server_match = _MCP_SERVER_RE.search(text)
    tool_match = _MCP_TOOL_RE.search(text)
    mcp_marker = re.search(r"\bmcp\b", text, re.IGNORECASE)

    # Явный MCP-вызов: нужен маркер "mcp" И server= И tool=.
    if mcp_marker and server_match and tool_match:
        return {
            "kind": "mcp",
            "server": server_match.group(1),
            "tool": tool_match.group(1),
            "arguments": _mcp_arguments(text),
        }

    if _MCP_INVOCATION_RE.search(text):
        # An "mcp" INVOCATION that is not a complete server=/tool= call: never
        # invent a default server — fail closed. A mere topical mention of
        # "MCP" (e.g. "MCP-сервер") is NOT an invocation and must not block a
        # legitimate TTS request.
        return None

    if not _TTS_VERB_RE.search(text):
        return None
    combined_imperative = bool(
        re.search(
            r"\bи\s+(?:озвуч\w*|произнес\w*|скажи\s+вслух|прочитай\s+вслух)\b",
            text,
            re.IGNORECASE,
        )
    )
    if _TTS_META_GUARD_RE.search(text) and not combined_imperative:
        return None

    # Явная пара server=/tool= без слова "mcp" — всё ещё MCP-вызов.
    if server_match and tool_match:
        return {
            "kind": "mcp",
            "server": server_match.group(1),
            "tool": tool_match.group(1),
            "arguments": _mcp_arguments(text),
        }

    arguments = _mcp_arguments(text)
    if not arguments.get("text"):
        spoken = _tts_text(text)
        if _EMAIL_REQUEST_RE.search(text):
            spoken = re.split(
                r"\bи\s+(?:отправь|отправить|отошли|перешли|скинь|пришли|вышли)\b",
                spoken,
                maxsplit=1,
                flags=re.IGNORECASE,
            )[0].strip()
        arguments["text"] = spoken
    if _EMAIL_REQUEST_RE.search(text) and "to" not in arguments:
        address = _EMAIL_ADDRESS_RE.search(text)
        arguments["to"] = (
            address.group(0)
            if address
            else os.environ.get("ANTIGONA_DELIVERY_EMAIL_TO", "")
        )
    return {
        "kind": "tts",
        "server": "",
        "tool": TTS_CONTRACT_TOOL,
        "arguments": arguments,
    }


_EMAIL_REQUEST_RE = re.compile(
    r"\b(?:отправь|отправить|отошли|перешли|скинь|пришли|вышли)\b.*"
    r"\b(?:на\s+)?(?:почту|email|e-?mail|мейл\b|[\w.+-]+@[\w-]+\.[\w.]+)",
    re.IGNORECASE | re.DOTALL,
)
_ATTACHMENT_PATH_RE = re.compile(
    r"\b([\w./\-]+\.(?:mp3|wav|ogg|txt|md|pdf|png|jpg|jpeg|mp4|zip|json|tar\.gz))\b",
    re.IGNORECASE,
)


def _parse_email_request(text: str) -> dict[str, Any] | None:
    """Извлечь запрос «отправь на почту» → параметры send_email.

    Возвращает ``{to, subject, body, attachment}``. Вложение: явный путь в
    тексте (``файл x.mp3``) либо пусто — мозг подставит последний артефакт
    активного флоу сессии. ``None`` — запрос не про почту.
    """
    if not text or not _EMAIL_REQUEST_RE.search(text):
        return None
    params: dict[str, Any] = {
        "to": os.environ.get("ANTIGONA_DELIVERY_EMAIL_TO", ""),
        "subject": "Antigona delivery",
        "body": text.strip(),
        "attachment": "",
    }
    path_match = _ATTACHMENT_PATH_RE.search(text)
    if path_match:
        params["attachment"] = path_match.group(1)
    return params


_KNOWN_INSTALL_REPLIES = {
    "edge-tts": (
        "edge-tts CLI уже установлен — озвучка идёт через встроенный инструмент "
        f"{TTS_CONTRACT_TOOL} (edge-tts CLI + ffmpeg): скажи «озвучь <текст>». "
        "MCP-сервер для этого не нужен."
    ),
    "mcp-edge-tts": (
        "Отдельный MCP-сервер edge-tts в этом деплое НЕ зарегистрирован, и он не "
        f"нужен: озвучка работает напрямую через {TTS_CONTRACT_TOOL} (edge-tts CLI "
        "+ ffmpeg). Скажи «озвучь <текст>»."
    ),
    "uv": "uv уже установлен (uv 0.12.3).",
}


def _known_install_reply(command: tuple[str, ...], text: str) -> str | None:
    """Осмысленный ответ на «install <известный пакет>» вместо shell-задачи.

    «Install edge-tts» из чата превращался в sandbox-задачу, которая упиралась
    в таймаут песочницы. Для известных, уже установленных компонентов отвечаем
    сразу, не создавая задачу. Неизвестные пакеты — None → обычная задача.
    """
    combined = " ".join(command) + " " + (text or "")
    match = re.search(
        r"\b(?:install|установи|поставь|ставь)\s+(?:pip\s+install\s+)?([\w.\-]+)",
        combined,
        re.IGNORECASE,
    )
    if not match:
        return None
    package = match.group(1).lower()
    return _KNOWN_INSTALL_REPLIES.get(package)


# ── Response types ────────────────────────────────────────────────────────────


class ResponseType:
    """Типы ответов мозга."""

    CONVERSATION = "conversation"
    TASK_ACCEPTED = "task_accepted"
    TASK_RESULT = "task_result"
    CLARIFICATION = "clarification"
    AUTH_REQUIRED = "auth_required"
    ERROR = "error"
    CONTROL = "control"


# ── File-send eligibility (task.file_send) ───────────────────────────────────
# Suffixes safe to deliver to the owner on a "скинь файл" request, and the
# secret suffixes that are always refused (mirrors registry._handle_send_file).
_FILE_SEND_SAFE_SUFFIXES = frozenset(
    {
        ".txt", ".md", ".json", ".py", ".log", ".sh", ".yaml", ".yml",
        ".toml", ".csv", ".ini", ".conf", ".html", ".xml",
        ".png", ".jpg", ".jpeg", ".gif", ".pdf",
    }
)
_FILE_SEND_SECRET_SUFFIXES = frozenset(
    {".pem", ".key", ".p12", ".pfx", ".crt", ".env", ".jks"}
)


def _is_secret_file(path: Path) -> bool:
    """True when ``path`` looks like a secret and must never be auto-sent.

    Fail-closed classifier used by every file-send branch (explicit target,
    auto-fallback, and :func:`_pick_newest_non_secret`). Matches on any of:

    * ``path.suffix`` in :data:`_FILE_SEND_SECRET_SUFFIXES` (suffix check);
    * an ``.env``-family name (``.env``, ``.env.production`` …) — by NAME;
    * a secret-looking file name even with an odd suffix
      (``id_rsa.pem.bak`` … — ``.pem/.key/.crt/.jks/.p12/.pfx`` in the name);
    * a ``secrets`` directory anywhere in the path.
    """
    name = path.name.lower()
    if path.suffix.lower() in _FILE_SEND_SECRET_SUFFIXES:
        return True
    if name == ".env" or name.startswith(".env"):
        return True
    if name.endswith((".pem", ".key", ".crt", ".jks", ".p12", ".pfx")):
        return True
    if any(part.lower() == "secrets" for part in path.parts):
        return True
    return False


def _is_within(base: Path, p: Path) -> bool:
    """True when ``p`` resolves to a path inside ``base`` (both resolved)."""
    try:
        p.resolve().relative_to(base.resolve())
        return True
    except ValueError:
        return False


def _resolve_send_target(workspace: Path, target: str) -> Path:
    """Return the candidate file inside ``workspace`` for an explicit target.

    Enforces the send fence for BOTH absolute and workspace-relative targets:

    * an absolute target must resolve INTO the workspace, else
      :class:`WorkspaceViolation` (``"path escapes workspace"``);
    * a workspace-relative target must pass :func:`validate_relative_path`
      (no ``../`` / unsafe components, no symlink path component);
    * the final resolved path must stay inside the workspace — this rejects a
      symlink that points outside.

    Raises:
        WorkspaceViolation / ValueError: when the target escapes the fence.
    """
    workspace = workspace.resolve()
    raw = Path(target)
    if raw.is_absolute():
        try:
            rel = raw.resolve(strict=False).relative_to(workspace)
        except ValueError as exc:
            raise WorkspaceViolation("path escapes workspace") from exc
        validate_relative_path(workspace, str(rel))
        candidate = workspace / rel
    else:
        validate_relative_path(workspace, target)
        candidate = workspace / target
    if not _is_within(workspace, candidate):
        raise WorkspaceViolation("path escapes workspace")
    return candidate


def _pick_newest_non_secret(workspace: Path) -> Path | None:
    """Newest workspace file eligible to be sent to the owner, or ``None``.

    Scans ``workspace`` non-recursively for regular files whose suffix is in
    the safe allow-list and which are not secrets (:func:`_is_secret_file`).
    Returns the file with the newest mtime, or ``None`` when the directory is
    missing or holds nothing eligible. Symlinks are skipped.
    """
    try:
        if not workspace.is_dir():
            return None
        candidates: list[Path] = []
        for entry in workspace.iterdir():
            if entry.is_symlink() or not entry.is_file():
                continue
            if _is_secret_file(entry):
                continue
            if entry.suffix.lower() not in _FILE_SEND_SAFE_SUFFIXES:
                continue
            candidates.append(entry)
        if not candidates:
            return None
        return max(candidates, key=lambda p: p.stat().st_mtime)
    except OSError:
        return None


@dataclass
class BrainResponse:
    """Унифицированный ответ от мозга Antigona.

    Attributes:
        text: Текст ответа для пользователя.
        response_type: Тип ответа (conversation, task_accepted, и т.д.).
        flow_id: ID потока задачи (если есть).
        intent: Классифицированное намерение.
        requires_approval: Требуется ли подтверждение.
        metadata: Дополнительные данные.
    """

    text: str
    response_type: str = ResponseType.CONVERSATION
    flow_id: str | None = None
    intent: str | None = None
    requires_approval: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


# ── Task backend (Execution Layer contract) ──────────────────────────────────


class TaskBackend(Protocol):
    """Внутренний контракт выполнения задач (серверный, без HTTP).

    Реализуется в каноническом Gateway через TaskRepository + DurableQueue.
    """

    async def submit_task(
        self,
        message: str,
        conversation_id: str = "",
        client: str = "cli",
        metadata: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        owner_id: str | None = None,
        correlation_id: str | None = None,
        tool_name: str | None = None,
        command: tuple[str, ...] = (),
        path: str | None = None,
        content: str | None = None,
        read_after_write: bool = False,
        run_after_write: bool = False,
        run_command: tuple[str, ...] = (),
        fix_after_run: bool = False,
        fix_content: str = "",
        fix_command: tuple[str, ...] = (),
        mcp_server: str = "",
        mcp_tool: str = "",
        mcp_arguments: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Создать задачу и вернуть dict с flow_id/id."""
        ...

    async def cancel_flow(self, flow_id: str) -> Any:
        """Отменить активную задачу."""
        ...

    async def get_flow(self, flow_id: str) -> Any:
        """Получить состояние задачи (flow view)."""
        ...

    async def steer_flow(self, flow_id: str, message: str) -> Any:
        """Скорректировать выполняющуюся задачу."""
        ...


# ── Control intents ───────────────────────────────────────────────────────────

_CONTROL_INTENTS = frozenset(
    {
        "task.steer",
        "task.correct",
        "task.continue",
        "task.cancel",
        "task.pause",
        "task.resume",
        "command.status",
        "flow.status",
    }
)


# ── AntigonaBrain ─────────────────────────────────────────────────────────────


class AntigonaBrain:
    """Единый мозг Antigona — единственная точка входа для обработки ввода.

    Живёт ТОЛЬКО в каноническом серверном runtime (Gateway). CLI и Telegram
    ходят в него через единый Gateway/Turn API и никогда не создают свой
    экземпляр ядра.

    Args:
        dialogue_engine: Единый DialogueEngine (создаётся сервером).
        session_repository: Репозиторий сессий (создаётся сервером).
        task_backend: Внутренний execution-бэкенд (TaskRepository + DurableQueue).
        db_path: Путь к БД сессий (fallback, если репозиторий не передан).
    """

    def __init__(
        self,
        dialogue_engine: DialogueEngine | None = None,
        session_repository: SessionRepository | None = None,
        task_backend: TaskBackend | None = None,
        db_path: str | None = None,
        workspace: str | Path | None = None,
    ) -> None:
        self._db_path = db_path
        self.task_backend = task_backend
        self._workspace = workspace

        # Единые экземпляры — один IntentRouter, один DialogueEngine, одна память
        self._intent_router = IntentRouter()
        self._session_repo = session_repository or SessionRepository(db_path=db_path)
        self._dialogue_engine = dialogue_engine or DialogueEngine(repository=self._session_repo, db_path=db_path)
        self._session_repo_connected = False

        # Summarizers per session (key = session_id)
        self._summarizers: dict[str, MemorySummarizer] = {}

        # Active flow per session (key = session_id)
        self._active_flows: dict[str, str] = {}

        # LOOP4 / DEFECT 2: последний путь записи по сессии. Нужен, чтобы
        # «прочитай этот же файл обратно» читал именно записанный файл, а не
        # дефолтный task_output.txt.
        self._last_write_paths: dict[str, str] = {}

        # Track LLM unavailable notification per session (don't spam)
        self._llm_unavailable_notified: set[str] = set()

        # Last text actually voiced per session — powers "покажи текст, который
        # ты озвучивал" without re-deriving it from the LLM.
        self._last_voiced_text: dict[str, str] = {}

        # Shared plugin registry + loader (lazy) — /plugins load/unload must
        # operate on persistent runtime state, not a throwaway per-call instance.
        self._plugin_registry: Any | None = None
        self._plugin_loader: Any | None = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def connect(self) -> None:
        """Инициализировать асинхронные ресурсы (подключение к БД)."""
        if not self._session_repo_connected:
            await self._session_repo.connect()
            self._session_repo_connected = True

    async def close(self) -> None:
        """Освободить асинхронные ресурсы."""
        if self._session_repo_connected:
            await self._session_repo.close()
            self._session_repo_connected = False
        if self._dialogue_engine is not None:
            close_result = self._dialogue_engine.close()
            if isawaitable(close_result):
                await close_result

    async def __aenter__(self) -> AntigonaBrain:
        await self.connect()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()

    # ── Main entry point ──────────────────────────────────────────────────────

    async def process(
        self,
        text: str,
        user_id: str,
        channel: str,
        session_id: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> BrainResponse:
        """Обработать пользовательский ввод и вернуть унифицированный ответ.

        Шаги:
            1. Определить session_id: ``{channel}:{user_id}`` если не передан
            2. Убедиться, что сессия существует в SessionRepository
            3. Классифицировать намерение через IntentRouter
            4. Маршрутизировать:
               a. conversation/question → DialogueEngine.reply()
               b. task → task_backend submit
               c. ambiguous → запрос уточнения
               d. control → управление активной задачей
            5. Сохранить сообщения (user + assistant) в сессию
            6. Вернуть BrainResponse

        Args:
            text: Текст пользователя.
            user_id: Идентификатор пользователя.
            channel: Канал (``"cli"``, ``"telegram"``, ...).
            session_id: Идентификатор сессии (авто если None).
            context: Дополнительный контекст.

        Returns:
            BrainResponse с текстом ответа и метаданными.
        """
        stripped = text.strip()
        if not stripped:
            return BrainResponse(
                text="",
                response_type=ResponseType.CONVERSATION,
            )

        # 1. Session ID — канонический формат {channel}:{user_id}
        sid = session_id or f"{channel}:{user_id}"

        # 2. Ensure session exists
        await self._ensure_session(sid, channel=channel, user_id=user_id)

        # Text retrieval (finding B): "покажи текст, который ты озвучивал" must
        # return the previously voiced text as ordinary text — never fall into
        # the file-write path and answer "Не удалось определить содержимое файла".
        retrieval = await self._maybe_answer_voiced_text_retrieval(stripped, sid)
        if retrieval is not None:
            return retrieval

        # 3. Get or create memory summarizer for this session
        summarizer = self._get_summarizer(sid)
        summarizer.push_user_turn(stripped, intent="pending")

        # 4. Classify intent
        router_context: dict[str, Any] = {"source": channel}
        active_flow = self._active_flows.get(sid)
        if active_flow:
            router_context["active_task_id"] = active_flow
        if context:
            router_context.update(context)
        # Pass recent conversation history to the router so short follow-ups
        # ("Сделай его попроще") can be resolved against prior context instead
        # of being force-clarified as ambiguous.
        try:
            if self._session_repo is not None and self._session_repo.db._conn is not None:
                _prev = await self._session_repo.get_messages(sid, limit=20)
                if _prev:
                    _hist = [m for m in _prev if m.get("role") in ("user", "assistant")]
                    if _hist:
                        router_context.setdefault("previous_messages", _hist)
                        for _m in reversed(_hist):
                            if _m.get("role") == "user":
                                router_context.setdefault(
                                    "active_topic", str(_m.get("content", ""))[:200]
                                )
                                break
        except Exception:
            logger.debug("could not load history for router context (sid=%s)", sid)
        # Per-call ownership context — passed explicitly to handlers, never
        # stored on the shared singleton (avoids cross-request races).
        owner_id = str((context or {}).get("owner_id") or "")
        correlation_id = str((context or {}).get("correlation_id") or "")
        turn_id = str((context or {}).get("turn_id") or "")

        # Router-level composition (live defect): a single message that both
        # asks for content AND to voice it must GENERATE first, then speak the
        # generated text — never the residual fragment of the directive.
        compose_voice = await self._maybe_handle_compose_and_voice(
            stripped,
            sid,
            owner_id=owner_id or None,
            channel=channel,
            correlation_id=correlation_id,
        )
        if compose_voice is not None:
            summarizer.push_assistant_turn(compose_voice.text)
            return compose_voice

        try:
            intent = self._intent_router.route(text=stripped, context=router_context)
        except Exception:
            logger.exception("IntentRouter.route() failed for session=%s", sid)
            intent = None

        # 5. Route by intent
        if _is_bare_shell_command(stripped):
            response = await self._handle_direct_shell(
                stripped, sid, intent, owner_id=owner_id or None,
                channel=channel, correlation_id=correlation_id, turn_id=turn_id
            )
        elif self._is_image_request(stripped) and not self._is_slash_command(stripped):
            # Deterministic free-image generation: detect an image-request phrase
            # ("нарисуй кота", "draw a cat") before falling to the LLM, which is
            # unreliable at emitting the tool call. Bypass the model entirely.
            response = await self._command_image(self._image_prompt_from(stripped))
        elif intent is not None and intent.intent == "question.system_time":
            # D2: дата/время — детерминированный ответ от системных часов,
            # не LLM (иначе модель выдумывает год).
            response = self._answer_system_time(intent)
        elif intent is None or self._is_conversation_intent(intent):
            response = await self._handle_conversation(
                stripped, sid, intent, owner_id=owner_id or None,
                channel=channel, correlation_id=correlation_id, turn_id=turn_id
            )
        elif intent.intent == "ambiguous.mixed_intent":
            if _extract_shell_command(stripped) is not None:
                # Чистая ascii-команда («ls -a», «uptime») — явный shell-запрос,
                # уточнение не нужно.
                response = await self._handle_task(
                    stripped, sid, intent,
                    owner_id=owner_id or None,
                    correlation_id=correlation_id or None,
                    channel=channel,
                )
            else:
                response = await self._handle_clarification(stripped, sid, intent)
        elif intent.intent in _CONTROL_INTENTS:
            response = await self._handle_control(
                stripped, sid, intent, owner_id=owner_id or None
            )
        elif intent.intent.startswith("command."):
            response = await self._handle_command(
                stripped, sid, intent, owner_id=owner_id or None
            )
        elif intent.intent == "task.file_read":
            response = await self._handle_file_read(
                stripped, sid, intent,
                owner_id=owner_id or None,
                correlation_id=correlation_id or None,
            )
        elif intent.intent == "task.file_send":
            response = await self._handle_file_send(
                stripped, sid, intent,
                owner_id=owner_id or None,
            )
        else:
            response = await self._handle_task(
                stripped, sid, intent,
                owner_id=owner_id or None,
                correlation_id=correlation_id or None,
                channel=channel,
            )

        # 6. Push assistant turn to summarizer
        summarizer.push_assistant_turn(response.text)

        return response

    # ── Intent classification helpers ─────────────────────────────────────────

    @staticmethod
    def _is_conversation_intent(intent: IntentDecision) -> bool:
        """Проверить, является ли намерение разговорным (answer-интент).

        conversation.* / question.* / analysis.* — всё это ответы, а не задачи:
        обычный вопрос никогда не должен превращаться в clarification или task.
        """
        return (
            intent.intent.startswith("conversation.")
            or intent.intent.startswith("question.")
            or intent.intent.startswith("analysis.")
        )

    @staticmethod
    def _answer_system_time(intent: IntentDecision) -> BrainResponse:
        """Ответить дату/время из системных часов (tool ``system.time``)."""
        from antigona.tools.system_time import format_system_time_reply

        answer = str(intent.entities.get("answer") or "") or format_system_time_reply()
        return BrainResponse(
            text=answer,
            response_type=ResponseType.CONVERSATION,
            intent=intent.intent,
            metadata={"tool_name": "system.time"},
        )

    # ── Conversation handler ──────────────────────────────────────────────────

    async def _handle_conversation(
        self,
        text: str,
        session_id: str,
        intent: IntentDecision | None,
        owner_id: str | None = None,
        channel: str = "",
        correlation_id: str = "",
        turn_id: str = "",
    ) -> BrainResponse:
        """Обработать разговорный запрос через единый DialogueEngine.

        Ядро владеет DialogueEngine напрямую — никаких HTTP-самокруговых вызовов.
        При недоступности LLM — честное сообщение (fail-closed, без фантомных
        TaskFlow и без скрытого локального движка).
        """
        try:
            turn_context: dict[str, Any] = {"intent": intent} if intent else {}
            if owner_id:
                turn_context["owner_id"] = owner_id
            if channel:
                turn_context["channel"] = channel
            if correlation_id:
                turn_context["correlation_id"] = correlation_id
            if turn_id:
                turn_context["turn_id"] = turn_id
            # If the user shared a link (article) and asked to study it, fetch
            # the page content and feed it to the LLM so it can actually
            # analyze the article instead of replying "I can't access web".
            enriched = text
            urls = _extract_urls(text)
            if urls:
                fetched = "\n".join(_fetch_web_content(u) for u in urls)
                if fetched:
                    enriched = (
                        "ИНСТРУКЦИЯ: ниже уже приведено ПОЛНОЕ содержимое страницы по ссылке, "
                        "которую просит изучить пользователь. НЕ говори, что не можешь получить "
                        "доступ к интернету или странице — контент УЖЕ предоставлен тебе ниже. "
                        "Проанализируй его и ответь по существу запроса.\n\n"
                        f"{fetched}\n"
                        f"[Запрос пользователя]:\n{text}"
                    )
            reply = await self._dialogue_engine.reply(
                text=enriched,
                session_id=session_id,
                context=turn_context,
            )
            # Сбросить флаг уведомления — LLM снова работает
            self._llm_unavailable_notified.discard(session_id)
            # ---- Integration: token accounting + observability (non-invasive) ----
            try:
                from antigona.core.observability import log_agent_turn
                from antigona.core.tokenizer import count_tokens
                log_agent_turn(
                    session_id=session_id, prompt=text, reply=reply,
                    tokens_in=count_tokens(text), tokens_out=count_tokens(reply),
                    model='deepseek', status='ok',
                )
            except Exception:
                pass

            # Truth contract: surface the REAL outcome of any tool the
            # conversation turn actually executed, so a denied/failed/partial
            # tool run is never presented or recorded as a success.
            conv_meta: dict[str, Any] = {}
            engine_outcome = getattr(
                self._dialogue_engine, "_last_tool_outcome", None
            )
            if isinstance(engine_outcome, str) and engine_outcome:
                conv_meta["tool_outcome"] = engine_outcome
            engine_error = getattr(self._dialogue_engine, "_last_tool_error", None)
            if isinstance(engine_error, str) and engine_error.strip():
                conv_meta["last_error"] = engine_error
            return BrainResponse(
                text=reply,
                response_type=ResponseType.CONVERSATION,
                intent=intent.intent if intent else None,
                metadata=conv_meta,
            )
        except Exception as exc:
            logger.warning(
                "DialogueEngine.reply() failed for session=%s: %s",
                session_id,
                exc,
            )
            # Честное сообщение — но только один раз за сессию
            if session_id not in self._llm_unavailable_notified:
                self._llm_unavailable_notified.add(session_id)
                return BrainResponse(
                    text=(
                        "Сейчас LLM-провайдер недоступен, поэтому я не могу "
                        "вести свободный диалог. Но я могу выполнять задачи — "
                        "просто опишите, что нужно сделать."
                    ),
                    response_type=ResponseType.ERROR,
                    intent=intent.intent if intent else None,
                )
            return BrainResponse(
                text="Опишите задачу — я выполню.",
                response_type=ResponseType.CONVERSATION,
                intent=intent.intent if intent else None,
            )

    async def _handle_direct_shell(
        self,
        text: str,
        session_id: str,
        intent: IntentDecision | None,
        owner_id: str | None = None,
        channel: str = "cli",
        correlation_id: str = "",
        turn_id: str = "",
    ) -> BrainResponse:
        """Execute a bare deterministic shell command (pwd, ls) directly via the canonical tool layer.

        Returns ordinary stdout in a conversation response without creating a TaskFlow.
        """
        import json

        from antigona.engine.unified_executor import ToolExecutionRequest, UnifiedToolExecutionLayer

        unified = getattr(self, "_unified_executor", None)
        if not unified:
            unified = UnifiedToolExecutionLayer()
            self._unified_executor = unified

        command = _extract_shell_command(text) or text.strip()

        # Bare deterministic commands (pwd, ls, ...) are SAFE-category and go
        # through the same PolicyEngine-checked "dialogue" path as any other
        # model-initiated shell request — never the PIN-gated owner-elevation
        # path. Gateway requests always carry a resolved owner_id (it is an
        # owner-scoped API), which is a Gateway-auth concept distinct from the
        # CLI's separate PIN owner-shell elevation; conflating the two would
        # make every bare pwd/ls in production hit "Owner shell denied".
        from antigona.security.auth_service import AuthService

        req = ToolExecutionRequest(
            tool_name="sandbox.shell",
            params={"command": command},
            requester="dialogue",
            channel=channel,
            user_id=owner_id or AuthService().owner_principal_id,
            session_id=session_id,
            correlation_id=correlation_id or f"direct-shell-{session_id}",
            turn_id=turn_id or correlation_id or f"direct-shell-{session_id}",
        )

        response: BrainResponse
        error_reason: str | None = None
        try:
            raw_res = await unified.execute(req)
            stdout = ""
            if isinstance(raw_res, str):
                try:
                    parsed = json.loads(raw_res)
                    if isinstance(parsed, dict):
                        if "error" in parsed and not parsed.get("success", False):
                            error_reason = str(parsed.get("error"))
                        else:
                            stdout = str(parsed.get("output") or parsed.get("result") or raw_res)
                    else:
                        stdout = raw_res
                except json.JSONDecodeError:
                    stdout = raw_res
            else:
                stdout = str(raw_res)

            if error_reason is not None:
                # Truth contract: a tool that failed/was denied must be recorded
                # with a REAL non-success outcome — never a bare CONVERSATION
                # that a thin client renders as "✅ Готово".
                response = BrainResponse(
                    text=f"Ошибка выполнения: {error_reason}",
                    response_type=ResponseType.CONVERSATION,
                    intent=intent.intent if intent else "task.shell",
                    metadata={
                        "tool_outcome": _classify_tool_outcome(error_reason),
                        "last_error": error_reason,
                    },
                )
            else:
                response = BrainResponse(
                    text=stdout.strip(),
                    response_type=ResponseType.CONVERSATION,
                    intent=intent.intent if intent else "task.shell",
                    metadata={"tool_outcome": "SUCCEEDED"},
                )
        except Exception as exc:
            logger.warning("Direct shell execution failed for '%s': %s", text, exc)
            error_reason = str(exc)
            response = BrainResponse(
                text=f"Ошибка выполнения команды '{text}': {exc}",
                response_type=ResponseType.ERROR,
                intent=intent.intent if intent else "task.shell",
                metadata={
                    "tool_outcome": _classify_tool_outcome(error_reason),
                    "last_error": error_reason,
                },
            )

        # Ground the conversation history in the *real* outcome — without this,
        # a later conversational turn asking about "ls"/"ps aux" has zero
        # record that a command ran at all, and the LLM has nothing to work
        # from but its own guesses. [TOOL_ERROR]-tagged failures follow the
        # existing persona contract (context/builder.py); successes are
        # recorded as the exact text the user was shown, so a follow-up like
        # "что там было" is answerable from real history, not invention.
        try:
            if self._session_repo is not None:
                grounding = (
                    f"[TOOL_ERROR] Команда '{command}' завершилась ошибкой: {error_reason}"
                    if error_reason is not None
                    else response.text
                )
                await self._session_repo.add_message(
                    session_id=session_id, role="user", content=text
                )
                await self._session_repo.add_message(
                    session_id=session_id, role="assistant", content=grounding
                )
        except Exception:
            logger.debug("Failed to persist direct-shell grounding for session=%s", session_id)

        return response

    # ── File-read handler (LOOP4 / DEFECT 2) ──────────────────────────────────


    async def _resolve_read_path(self, session_id: str, intent: IntentDecision) -> str:
        """Определить, ЧТО читать.

        Порядок: явный путь из запроса → путь последней write-задачи этой
        сессии → артефакт активного потока. Ничего из этого нет → пустая
        строка, и вызывающий отвечает уточнением (никогда — записью).
        """
        explicit = str((intent.entities or {}).get("path") or "").strip()
        if explicit:
            return explicit
        remembered = self._last_write_paths.get(session_id, "").strip()
        if remembered:
            return remembered
        return (await self._latest_artifact_path(session_id)).strip()

    async def _handle_file_read(
        self,
        text: str,
        session_id: str,
        intent: IntentDecision,
        owner_id: str | None = None,
        correlation_id: str | None = None,
    ) -> BrainResponse:
        """Зарегистрировать read-задачу для исполнения оркестратором.

        Контракт (LOOP4 / DEFECT 2 / EXACTLY-ONCE READ):

        * brain НЕ читает файл инлайново (0 вызовов read-тула при регистрации);
        * резолвит путь: запрос → _last_write_paths сессии → артефакт активного потока;
          если путь не определён → clarification, задача НЕ создаётся;
        * проверяет workspace-границу (fail-closed) — путь наружу отвергается,
          задача НЕ создаётся;
        * регистрирует durable-задачу tool_name="workspace.read_text" с path
          (content=None; draft-логика D3 на read НЕ срабатывает);
        * ровно ОДНО чтение на задачу выполняется в оркестраторе.
        """
        if self.task_backend is None:
            return BrainResponse(
                text=(
                    "Gateway недоступен. Задача не может быть выполнена. "
                    "Убедитесь, что Gateway запущен (antigona-gateway)."
                ),
                response_type=ResponseType.ERROR,
                intent=intent.intent,
            )

        target_path = await self._resolve_read_path(session_id, intent)
        if not target_path:
            return BrainResponse(
                text=(
                    "Не понял, какой файл прочитать. "
                    "Назови имя файла — например «прочитай файл notes.txt»."
                ),
                response_type=ResponseType.CLARIFICATION,
                intent=intent.intent,
            )

        workspace_raw = self._workspace or os.getenv("ANTIGONA_WORKSPACE") or "./workspace"
        workspace = Path(workspace_raw).resolve()
        try:
            validate_relative_path(workspace, target_path)
            candidate = workspace / target_path
            if candidate.is_symlink():
                raise WorkspaceViolation("symlink target forbidden")
            target = candidate.resolve(strict=False)
            target.relative_to(workspace)
        except (WorkspaceViolation, ValueError, OSError) as exc:
            logger.warning(
                "workspace.read_text refused path=%r for session=%s: %s",
                target_path,
                session_id,
                exc,
            )
            return BrainResponse(
                text=f"Не удалось прочитать «{target_path}»: {exc}",
                response_type=ResponseType.ERROR,
                intent=intent.intent,
                metadata={
                    "tool_name": WORKSPACE_READ_TEXT,
                    "path": target_path,
                    "error": str(exc),
                },
            )

        flow_id: str | None = None
        try:
            submitted = await self.task_backend.submit_task(
                message=text,
                conversation_id=session_id,
                client="core",
                owner_id=owner_id,
                correlation_id=correlation_id,
                tool_name=WORKSPACE_READ_TEXT,
                path=target_path,
                content=None,
            )
            if isinstance(submitted, dict):
                flow_id = str(submitted.get("flow_id") or submitted.get("id") or "") or None
            else:
                flow_id = (
                    str(
                        getattr(submitted, "flow_id", None)
                        or getattr(submitted, "id", None)
                        or ""
                    )
                    or None
                )
            if flow_id:
                self._active_flows[session_id] = flow_id
        except Exception as exc:
            logger.warning(
                "read task registration failed for session=%s: %s", session_id, exc
            )
            return BrainResponse(
                text="Не удалось отправить задачу на выполнение.",
                response_type=ResponseType.ERROR,
                intent=intent.intent,
            )

        return BrainResponse(
            text="Задача принята и выполняется.",
            response_type=ResponseType.TASK_ACCEPTED,
            flow_id=flow_id,
            intent=intent.intent,
            requires_approval=False,
            metadata={
                "tool_name": WORKSPACE_READ_TEXT,
                "path": target_path,
            },
        )

    # ── File-send handler (task.file_send) ───────────────────────────────────

    async def _handle_file_send(
        self,
        text: str,
        session_id: str,
        intent: IntentDecision,
        owner_id: str | None = None,
    ) -> BrainResponse:
        """Отдать УЖЕ существующий файл владельцу в чат («скинь файл X»).

        Авторизация этой inline-ветки — СТРОГИЙ fail-closed gate по личности
        владельца (``OwnerIdentity``, только owner-id) плюс отказ по секретам.
        Ветка НЕ обходит этот gate и НЕ имеет отдельного approval/chokepoint:
        без опознанного владельца не отправляется ничего.

        Контракт:

        * инициировать отправку может ТОЛЬКО владелец (OwnerIdentity,
          fail-closed) — иначе ERROR, ничего не отправляется;
        * конкретное имя из запроса резолвится в workspace через
          ``_resolve_send_target`` (по имени или по относительному пути строго
          в границах workspace; абсолютный путь обязан резолвиться ВНУТРЬ
          workspace, симлинк наружу отклоняется); если файла нет —
          CLARIFICATION, файл НЕ выдумывается;
        * ``auto`` — берётся самый свежий безопасный артефакт workspace, затем
          артефакт активного потока (через тот же fence + secret-фильтр);
          секреты (.pem/.key/.env/...) исключены и по расширению, и по имени;
        * реальная отправка — ``registry._handle_send_file`` на chat_id
          владельца.
        """
        workspace_raw = self._workspace or os.getenv("ANTIGONA_WORKSPACE") or "./workspace"
        workspace = Path(str(workspace_raw)).resolve()

        # ── Owner auth gate (fail-closed) ───────────────────────────────────
        from antigona.security.owner_identity import OwnerIdentity

        identity = OwnerIdentity()
        owner_ok = False
        if owner_id and owner_id.strip().lstrip("-").isdigit():
            owner_ok = identity.is_owner(int(owner_id))
        if not owner_ok:
            return BrainResponse(
                text="Отправка файлов разрешена только владельцу.",
                response_type=ResponseType.ERROR,
                intent=intent.intent,
            )

        target = str((intent.entities or {}).get("file_target") or "").strip()
        final_path: Path | None = None

        if target and target != "auto":
            try:
                candidate = _resolve_send_target(workspace, target)
            except (WorkspaceViolation, ValueError) as exc:
                return BrainResponse(
                    text=f"Не удалось отправить «{target}»: {exc}",
                    response_type=ResponseType.ERROR,
                    intent=intent.intent,
                )
            if candidate.exists() and candidate.is_file():
                final_path = candidate
            if final_path is None:
                return BrainResponse(
                    text=f"Файл «{target}» не найден — отправлять нечего.",
                    response_type=ResponseType.CLARIFICATION,
                    intent=intent.intent,
                )
        else:
            picked = _pick_newest_non_secret(workspace)
            if picked is not None:
                final_path = picked
            else:
                fallback = (await self._latest_artifact_path(session_id)).strip()
                if fallback:
                    try:
                        fb_candidate = _resolve_send_target(workspace, fallback)
                        if (
                            fb_candidate.exists()
                            and fb_candidate.is_file()
                            and not _is_secret_file(fb_candidate)
                        ):
                            final_path = fb_candidate
                    except (WorkspaceViolation, ValueError, OSError):
                        final_path = None
            if final_path is None:
                return BrainResponse(
                    text="Не нашёл файл для отправки",
                    response_type=ResponseType.CLARIFICATION,
                    intent=intent.intent,
                )

        # ── Secret refusal — by suffix AND by name (mirror _handle_send_file) ─
        if _is_secret_file(final_path):
            return BrainResponse(
                text=(
                    "Отправка секретных файлов (.pem/.key/.env/...) требует "
                    "явного подтверждения владельца."
                ),
                response_type=ResponseType.ERROR,
                intent=intent.intent,
            )

        from antigona.tools.registry import _handle_send_file

        sres = await _handle_send_file(
            path=str(final_path),
            caption="",
            chat_id=str(owner_id),
        )
        try:
            parsed = json.loads(sres)
        except (ValueError, TypeError):
            parsed = {}
        if parsed.get("success") or parsed.get("ok"):
            return BrainResponse(
                text=f"✅ Отправил файл: {final_path.name}",
                response_type=ResponseType.CONVERSATION,
                intent=intent.intent,
            )
        err = str(parsed.get("error") or (sres if isinstance(sres, str) else "") or "неизвестная ошибка")
        return BrainResponse(
            text=f"Не удалось отправить файл: {err}",
            response_type=ResponseType.ERROR,
            intent=intent.intent,
        )

    # ── Clarification handler ─────────────────────────────────────────────────

    async def _handle_clarification(
        self,
        text: str,
        session_id: str,
        intent: IntentDecision,
    ) -> BrainResponse:
        """Запросить уточнение у пользователя."""
        if (
            intent.reason_code == "multi_file_requires_spec"
            and intent.entities
            and intent.entities.get("paths")
        ):
            paths_str = ", ".join(intent.entities["paths"])
            clarify_text = f"Обнаружено несколько файлов: {paths_str}. Уточните, что именно нужно сделать."
        else:
            clarify_text = (
                "Уточните, пожалуйста, что нужно сделать: "
                "это вопрос, задача или команда? "
                "Например: «создай файл …», «проверь …» или «расскажи про …»."
            )
        return BrainResponse(
            text=clarify_text,
            response_type=ResponseType.CLARIFICATION,
            intent=intent.intent,
        )

    # ── Control handler ───────────────────────────────────────────────────────

    async def _latest_artifact_path(self, session_id: str) -> str:
        """Path of the newest artifact of the session's active flow (for email).

        Returns the artifact path (workspace-relative) or an empty string when
        there is no active flow / no artifact — the send_email executor then
        simply sends the message without an attachment.
        """
        flow_id = self._active_flows.get(session_id)
        if not flow_id or self.task_backend is None:
            return ""
        try:
            flow = await self.task_backend.get_flow(flow_id)
            artifacts = getattr(flow, "artifacts", None)
            if isinstance(flow, dict):
                artifacts = flow.get("artifacts")
            if artifacts:
                first = artifacts[0]
                path = getattr(first, "path", None)
                if path is None and isinstance(first, dict):
                    path = first.get("path")
                return str(path or "")
        except Exception:
            pass
        return ""

    async def _handle_control(
        self,
        text: str,
        session_id: str,
        intent: IntentDecision,
        owner_id: str | None = None,
    ) -> BrainResponse:
        """Управление активной задачей (cancel, pause, status и т.д.)."""
        active_flow = self._active_flows.get(session_id)
        if not active_flow:
            return BrainResponse(
                text=(
                    "Нет активной задачи для этого действия. "
                    "Опишите задачу, и я начну её выполнение."
                ),
                response_type=ResponseType.CONTROL,
                intent=intent.intent,
            )

        if self.task_backend is None:
            return BrainResponse(
                text="Gateway недоступен для управления задачей.",
                response_type=ResponseType.ERROR,
                intent=intent.intent,
            )

        # Cancel
        if intent.intent in ("task.cancel",):
            try:
                await self.task_backend.cancel_flow(active_flow)
                self._active_flows.pop(session_id, None)
                return BrainResponse(
                    text=f"Задача {active_flow} отменена.",
                    response_type=ResponseType.CONTROL,
                    intent=intent.intent,
                    flow_id=active_flow,
                )
            except Exception as exc:
                logger.warning("cancel_flow failed: %s", exc)
                return BrainResponse(
                    text="Не удалось отменить задачу.",
                    response_type=ResponseType.ERROR,
                    intent=intent.intent,
                )

        # Status
        if intent.intent in ("command.status", "flow.status"):
            try:
                flow_view = await self.task_backend.get_flow(active_flow)
                status = getattr(flow_view, "status", "unknown")
                return BrainResponse(
                    text=f"Статус задачи {active_flow}: {status}",
                    response_type=ResponseType.CONTROL,
                    intent=intent.intent,
                    flow_id=active_flow,
                    metadata={"flow_view": flow_view},
                )
            except Exception as exc:
                logger.warning("get_flow failed: %s", exc)
                return BrainResponse(
                    text="Не удалось получить статус задачи.",
                    response_type=ResponseType.ERROR,
                    intent=intent.intent,
                )

        # Steer / correct / continue — через единый backend
        try:
            steered = await self.task_backend.steer_flow(active_flow, text.strip())
            status = getattr(steered, "status", "unknown")
            return BrainResponse(
                text="Задача скорректирована.",
                response_type=ResponseType.CONTROL,
                intent=intent.intent,
                flow_id=active_flow,
                metadata={"flow_view": steered, "status": status},
            )
        except Exception as exc:
            logger.warning("steer_flow failed: %s", exc)
            return BrainResponse(
                text="Не удалось скорректировать задачу.",
                response_type=ResponseType.ERROR,
                intent=intent.intent,
                flow_id=active_flow,
            )

    # ── Task handler ──────────────────────────────────────────────────────────


    # ── Deterministic command handlers ─────────────────────────────────────────
    # Registered slash commands (/model, /providers, /bot, /keys, /help) are
    # answered by deterministic handlers — NEVER by the generic LLM dialogue —
    # so the LLM cannot invent semantics for a registered command. All of these
    # are read-only: they create no TaskFlow and require no approval.

    async def _handle_command(
        self,
        text: str,
        session_id: str,
        intent: IntentDecision,
        owner_id: str | None = None,
    ) -> BrainResponse:
        kind = intent.intent
        cmd_name = ""
        if intent.reason_code and intent.reason_code.startswith("slash_command_"):
            cmd_name = intent.reason_code[len("slash_command_"):]
        try:
            if kind == "command.model_select":
                return self._command_model()
            if kind == "command.providers":
                return self._command_providers()
            if kind == "command.bot":
                return self._command_bot()
            if kind == "command.keys":
                return self._command_keys()
            if kind == "command.help":
                return self._command_help()
            if kind == "command.health":
                return self._command_health()
            if kind == "command.session":
                return await self._command_session(session_id)
            if kind == "command.history":
                return await self._command_history(session_id)
            if kind == "command.memory":
                return await self._command_memory(text, session_id)
            if kind == "command.commands":
                return self._command_commands()
            if kind == "command.sysinfo":
                return self._command_sysinfo()
            if kind == "command.web":
                return await self._command_web(text)
            if kind == "command.ollama":
                return await self._command_ollama(text)
            if kind == "command.image":
                return await self._command_image(text)
            if kind == "command.tts":
                return await self._command_tts(text)
            if kind == "command.install":
                return await self._command_install(text, owner_id)
            if kind == "command.install_auto":
                return await self._command_install_auto(text, session_id, owner_id)
            if kind == "command.mcp":
                return await self._command_mcp(text)
            if kind == "command.plugins":
                return await self._command_plugins(text)
            if kind == "command.skills":
                return await self._command_skills(text)
            if kind == "command.cli":
                return self._command_cli()
            if kind == "command.hermes":
                return await self._command_hermes(text, session_id)
            if kind == "command.get":
                return await self._command_get(text, session_id)
            if kind == "command.steer":
                return await self._command_steer(text, session_id)
            if kind == "command.list":
                return await self._command_list()
            if kind in ("command.approvals", "command.approve", "command.deny"):
                return await self._command_approvals(text)
        except Exception as exc:
            logger.warning("command handler %s failed: %s", kind, exc)
            return BrainResponse(
                text="Не удалось выполнить команду.",
                response_type=ResponseType.ERROR,
                intent=kind,
            )
        return BrainResponse(
            text=f"Команда /{cmd_name or kind} не поддерживается.",
            response_type=ResponseType.CONVERSATION,
            intent=kind,
        )

    def _command_model(self) -> BrainResponse:
        from antigona.providers.resolver import ProviderResolver

        info = ProviderResolver.get_active_info()
        if info.status != "active":
            return BrainResponse(
                text="Модель не сконфигурирована (runtime provider unresolved).",
                response_type=ResponseType.CONVERSATION,
                intent="command.model_select",
            )
        return BrainResponse(
            text=f"🤖 Текущая модель: {info.model_name}\nПровайдер: {info.display_name} ({info.provider_name})\nEndpoint: {info.base_url}",
            response_type=ResponseType.CONVERSATION,
            intent="command.model_select",
        )

    def _command_providers(self) -> BrainResponse:
        from antigona.providers.resolver import ProviderResolver

        info = ProviderResolver.get_active_info()
        active = (
            f"{info.display_name} ({info.provider_name}) — {info.model_name}"
            if info.status == "active"
            else "runtime provider unresolved"
        )
        lines = [
            "🏭 Активный LLM-провайдер:",
            f"- {active}",
            f"  endpoint: {info.base_url}",
            f"  endpoint class: {info.endpoint_class}",
        ]
        return BrainResponse(
            text="\n".join(lines), response_type=ResponseType.CONVERSATION, intent="command.providers"
        )

    def _command_bot(self) -> BrainResponse:
        import os as _os
        token = _os.getenv("TELEGRAM_BOT_TOKEN", "")
        owner = _os.getenv("ANTIGONA_OWNER_ID", "—")
        chat = _os.getenv("ANTIGONA_DELIVERY_TELEGRAM_CHAT_ID", "—")
        status = "активен" if token else "не сконфигурирован"
        return BrainResponse(
            text=f"🤖 Telegram-бот: {status}\nOwner ID: {owner}\nDelivery chat: {chat}",
            response_type=ResponseType.CONVERSATION,
            intent="command.bot",
        )

    def _command_keys(self) -> BrainResponse:
        # Key status WITHOUT exposing secret values.
        import os as _os

        from antigona.core.paths import secrets_dir

        def _configured(env_name: str, secret_rel: str | None = None) -> bool:
            if _os.getenv(env_name):
                return True
            if secret_rel:
                try:
                    _p = secrets_dir() / secret_rel
                    return bool(_p.read_text().strip())
                except OSError:
                    return False
            return False

        deepseek = _configured("DEEPSEEK_API_KEY", "deepseek.json")
        openrouter = _configured("OPENROUTER_API_KEY")
        lines = [
            "🔑 Состояние ключей:",
            f"- deepseek: {'сконфигурирован' if deepseek else 'отсутствует'}",
            f"- openrouter: {'сконфигурирован' if openrouter else 'отсутствует'}",
        ]
        return BrainResponse(
            text="\n".join(lines), response_type=ResponseType.CONVERSATION, intent="command.keys"
        )

    def _command_help(self) -> BrainResponse:
        try:
            from antigona.core.command_registry import commands_for_channel
            specs = commands_for_channel("cli")
            lines = ["📋 Доступные команды:"] + [f"  /{sp.name} — {sp.description}" for sp in specs]
            return BrainResponse(
                text="\n".join(lines), response_type=ResponseType.CONVERSATION, intent="command.help"
            )
        except Exception:
            return BrainResponse(
                text="📋 Справка недоступна.", response_type=ResponseType.CONVERSATION, intent="command.help"
            )

    # ── Extended command handlers (owner-requested 2026-08-10) ─────────────
    def _strip_cmd(self, text: str) -> str:
        """Remove a leading slash-command token from free text."""
        return re.sub(r"^\s*/\S+", "", text or "").strip()

    def _command_health(self) -> BrainResponse:
        import os as _os
        import subprocess as _sp

        def _probe(url: str) -> str:
            import urllib.error
            try:
                # Bypass proxy for localhost probes (gateway/verifier are local).
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                with opener.open(url, timeout=3) as r:
                    return "ok" if r.status < 500 else f"HTTP {r.status}"
            except urllib.error.HTTPError as h:
                # HTTP 4xx still means the server is up.
                return "ok" if h.code < 500 else f"HTTP {h.code}"
            except Exception:
                return "down"

        # We are running inside the Gateway — it is up by definition. Probing
        # itself synchronously from the turn handler would deadlock the event loop.
        gw = "ok"
        vr = _probe("http://127.0.0.1:8091/")
        ol = _probe("http://127.0.0.1:11434/api/version")
        wk = "ok" if _sp.run(["pgrep", "-f", "antigona.worker"], capture_output=True).returncode == 0 else "down"
        try:
            st = _os.statvfs("/")
            free = st.f_bavail * st.f_frsize / 1e9
        except Exception:
            free = 0.0
        lines = [
            "🩺 Здоровье Antigona:",
            f"• gateway :8090 — {gw}",
            f"• verifier :8091 — {vr}",
            f"• worker — {wk}",
            f"• ollama :11434 — {ol}",
            f"• диск свободно: {free:.1f} ГБ",
        ]
        return BrainResponse(
            text="\n".join(lines), response_type=ResponseType.CONVERSATION, intent="command.health"
        )

    async def _command_session(self, session_id: str) -> BrainResponse:
        count: int = 0
        try:
            msgs = await self._session_repo.get_messages(session_id, limit=100)
            count = len(msgs) if msgs else 0
        except Exception:
            pass
        return BrainResponse(
            text=f"💬 Сессия: {session_id}\nСообщений в окне: {count}",
            response_type=ResponseType.CONVERSATION,
            intent="command.session",
        )

    async def _command_history(self, session_id: str, limit: int = 10) -> BrainResponse:
        try:
            msgs = await self._session_repo.get_messages(session_id, limit=max(5, min(limit, 30)))
            lines = ["📜 История сессии (последние):"]
            for m in msgs[-limit:]:
                role = str(m.get("role", "?")) if isinstance(m, dict) else "?"
                content = str(m.get("content", m.get("text", ""))) if isinstance(m, dict) else str(m)
                lines.append(f"• {role}: {content[:120].replace(chr(10), ' ')}")
            if len(lines) == 1:
                return BrainResponse(
                    text="История пуста.", response_type=ResponseType.CONVERSATION, intent="command.history"
                )
            return BrainResponse(
                text="\n".join(lines), response_type=ResponseType.CONVERSATION, intent="command.history"
            )
        except Exception as exc:
            return BrainResponse(
                text=f"История недоступна: {exc}", response_type=ResponseType.CONVERSATION, intent="command.history"
            )

    async def _command_memory(self, text: str, session_id: str) -> BrainResponse:
        from antigona.core.memory_repository import MemoryRepository
        if self.task_backend is None or not hasattr(self.task_backend, "database"):
            return BrainResponse(
                text="Память недоступна (нет базы данных).",
                response_type=ResponseType.CONVERSATION,
                intent="command.memory",
            )
        repo = MemoryRepository(self.task_backend.database)
        owner = "default"
        payload = self._strip_cmd(text)
        if payload:
            try:
                entry = repo.remember(owner_id=owner, content=payload, title=payload[:60])
                return BrainResponse(
                    text=f"🧠 Запомнил (#{entry.get('id', '')}): {payload[:120]}",
                    response_type=ResponseType.CONVERSATION,
                    intent="command.memory",
                )
            except Exception as exc:
                return BrainResponse(
                    text=f"Не удалось запомнить: {exc}",
                    response_type=ResponseType.CONVERSATION,
                    intent="command.memory",
                )
        try:
            entries = repo.list_entries(owner_id=owner, limit=10)
            if not entries:
                return BrainResponse(
                    text="🧠 Память пуста. Используй /memory <текст>, чтобы запомнить.",
                    response_type=ResponseType.CONVERSATION,
                    intent="command.memory",
                )
            lines = ["🧠 Память агента:"]
            for e in entries:
                lines.append(f"• {str(e.get('title', ''))[:70]}")
            return BrainResponse(
                text="\n".join(lines), response_type=ResponseType.CONVERSATION, intent="command.memory"
            )
        except Exception as exc:
            return BrainResponse(
                text=f"Не удалось прочитать память: {exc}",
                response_type=ResponseType.CONVERSATION,
                intent="command.memory",
            )

    def _command_commands(self) -> BrainResponse:
        return self._command_help()

    def _command_sysinfo(self) -> BrainResponse:
        import os as _os
        import platform
        mem_total = mem_avail = 0.0
        load = "n/a"
        uptime = 0.0
        free = 0.0
        try:
            for line in open("/proc/meminfo"):
                k, _, v = line.partition(":")
                if k == "MemTotal":
                    mem_total = int(v.split()[0]) / 1e6
                elif k == "MemAvailable":
                    mem_avail = int(v.split()[0]) / 1e6
            load = ",".join(f"{x:.2f}" for x in _os.getloadavg())
            uptime = float(open("/proc/uptime").read().split()[0])
            st = _os.statvfs("/")
            free = st.f_bavail * st.f_frsize / 1e9
        except Exception:
            pass
        lines = [
            "🖥 Система:",
            f"• ОС: {platform.system()} {platform.release()}",
            f"• Python: {platform.python_version()}",
            f"• Память: {mem_avail:.1f}/{mem_total:.1f} ГБ доступно",
            f"• Load avg: {load}",
            f"• Диск свободно: {free:.1f} ГБ",
            f"• Uptime: {int(uptime) // 3600} ч {int(uptime) % 3600 // 60} мин",
        ]
        return BrainResponse(
            text="\n".join(lines), response_type=ResponseType.CONVERSATION, intent="command.sysinfo"
        )

    async def _command_web(self, text: str) -> BrainResponse:
        from antigona.tools.integrations import _handle_web_search
        query = self._strip_cmd(text)
        if not query:
            return BrainResponse(
                text="Использование: /web <запрос> — поиск в интернете.",
                response_type=ResponseType.CONVERSATION,
                intent="command.web",
            )
        try:
            res = await _handle_web_search(query=query, max_results=5)
            data = json.loads(res)
            if not data.get("success"):
                return BrainResponse(
                    text=f"Поиск не удался: {data.get('error')}",
                    response_type=ResponseType.CONVERSATION,
                    intent="command.web",
                )
            items = data.get("items", [])
            if not items:
                return BrainResponse(
                    text=f"По запросу «{query}» ничего не найдено.",
                    response_type=ResponseType.CONVERSATION,
                    intent="command.web",
                )
            lines = [f"🔎 По запросу «{query}»:"]
            for it in items:
                lines.append(f"• {str(it.get('title', ''))[:70]}\n  {it.get('url', '')}")
            return BrainResponse(
                text="\n".join(lines), response_type=ResponseType.CONVERSATION, intent="command.web"
            )
        except Exception as exc:
            return BrainResponse(
                text=f"Поиск не удался: {exc}",
                response_type=ResponseType.CONVERSATION,
                intent="command.web",
            )

    _IMAGE_REQUEST_RE = re.compile(
        r"^(?:нарисуй|нарисуй мне|нарисуй-ка|сгенерируй|создай картинку|создай изображение|"
        r"сделай картинку|сделай изображение|нарисуй картинку|изобрази)\b"
        r"|^(?:draw|draw me|generate image|generate a picture|create an image|"
        r"create a picture|make an image|make a picture)\b",
        re.IGNORECASE,
    )

    @staticmethod
    def _is_image_request(text: str) -> bool:
        return bool(AntigonaBrain._IMAGE_REQUEST_RE.search(text))

    @staticmethod
    def _is_slash_command(text: str) -> bool:
        return bool(text.strip().startswith("/"))

    @staticmethod
    def _image_prompt_from(text: str) -> str:
        """Strip the leading image verb, leaving the subject as the prompt."""
        stripped = AntigonaBrain._IMAGE_REQUEST_RE.sub("", text).strip(" :,-")
        return stripped or text.strip()

    async def _command_image(self, text: str) -> BrainResponse:
        """Generate a free image via the existing ImageGenerator (Pollinations.ai).

        Thin command over the registered GENERATE_IMAGE handler — no duplicate logic.
        Usage: /image <prompt>.
        """
        prompt = self._strip_cmd(text).strip()
        if not prompt:
            return BrainResponse(
                text="Использование: /image <промпт> — бесплатная генерация картинки (Pollinations.ai).",
                response_type=ResponseType.CONVERSATION,
                intent="command.image",
            )
        try:
            import json as _json

            from antigona.tools.registry import _handle_generate_image, _handle_send_file
            res = await _handle_generate_image(prompt=prompt)
            data = _json.loads(res)
            if not data.get("success"):
                return BrainResponse(
                    text=f"Не удалось сгенерить картинку: {data.get('error')}",
                    response_type=ResponseType.ERROR,
                    intent="command.image",
                )
            path = data.get("path")
            # Deliver the generated image to Telegram (delivery chat).
            delivered = False
            note = ""
            if path:
                try:
                    sres = await _handle_send_file(
                        path=path, caption=f"🖼 {prompt[:100]}"
                    )
                    sdata = _json.loads(sres)
                    delivered = bool(sdata.get("success") or sdata.get("ok") or sdata.get("mock"))
                    if sdata.get("error"):
                        note = f"\n⚠️ Не доставил в Telegram: {sdata['error']}"
                except Exception as exc:
                    note = f"\n⚠️ Не доставил в Telegram: {exc}"
            delivered_txt = "✅ Отправил в Telegram" if delivered else ""
            return BrainResponse(
                text=f"🖼 Сгенерил картинку (Pollinations.ai):\n`{path}`\n"
                     f"{delivered_txt}{note}",
                response_type=ResponseType.CONVERSATION,
                intent="command.image",
                metadata={"image_path": path, "delivered": delivered},
            )
        except Exception as exc:
            return BrainResponse(
                text=f"Не удалось сгенерить картинку: {exc}",
                response_type=ResponseType.ERROR,
                intent="command.image",
            )

    async def _last_assistant_text(self, session_id: str) -> str:
        """Most recent substantive assistant turn text (for TTS references).

        Skips completion confirmations, voice markers and recorded action
        notes so that "озвучь рассказ" resolves to the POEM, not to the
        previous "🎙 Озвучил текст..." acknowledgement.
        """
        if self._session_repo is None:
            return ""
        try:
            msgs = await self._session_repo.get_messages(session_id, limit=40)
        except Exception:
            logger.debug("tts: history load failed for session=%s", session_id)
            return ""
        for m in reversed(msgs):
            if m.get("role") != "assistant":
                continue
            content = str(m.get("content") or "").strip()
            if not content:
                continue
            if "\u27ea" + "voice:" in content or content.startswith("🎙"):
                continue
            if content.startswith("[ACTION]") or content.startswith("[TOOL_ERROR]"):
                continue
            return content
        return ""

    def _read_workspace_file(self, name: str) -> str | None:
        """Read a named workspace file safely; ``None`` when it cannot be read."""
        try:
            workspace = Path(
                self._workspace or os.getenv("ANTIGONA_WORKSPACE") or "./workspace"
            ).resolve()
            validate_relative_path(workspace, name)
            candidate = workspace / name
            if candidate.is_symlink():
                return None
            target = candidate.resolve(strict=False)
            target.relative_to(workspace)
            if not target.is_file():
                return None
            return target.read_text(encoding="utf-8", errors="replace").strip()
        except (WorkspaceViolation, ValueError, OSError):
            return None

    async def _resolve_tts_target(
        self,
        *,
        source_message: str,
        candidate: str,
        session_id: str,
    ) -> tuple[str, str | None]:
        """Resolve the text to SPEAK — never the raw inbound command.

        Resolution order:
          1. explicit quoted text after the verb ("озвучь \u00abПривет\u00bb");
          2. explicit literal after the verb and a colon ("озвучь: Привет");
          3. a reference to earlier assistant output ("озвучь рассказ") or an
             empty target -> the most recent assistant turn text;
          4. a named workspace file -> its contents;
          5. otherwise the residual body, when it genuinely carries the text.
        Returns ``(text, None)`` or ``("", actionable_refusal)``.
        """
        message = (source_message or "").strip()
        quoted = _TTS_QUOTED_RE.search(message)
        if quoted and quoted.group("text").strip():
            return quoted.group("text").strip(), None
        colon = _TTS_COLON_RE.search(message)
        if colon and colon.group("text").strip():
            return colon.group("text").strip(), None
        tail = (candidate or "").strip()
        if (
            not tail
            or _TTS_REFERENCE_RE.match(tail)
            or _TTS_VOICE_NOUN_TAIL_RE.match(tail)
        ):
            prior = await self._last_assistant_text(session_id)
            if prior:
                return prior, None
            return "", (
                "Не понял, какой текст озвучить: в этом диалоге нет предыдущего "
                "текста, на который можно сослаться. Напишите: «озвучь: <текст>» "
                "или «озвучь файл story.txt»."
            )
        file_match = _TTS_FILE_RE.search(tail)
        if file_match:
            name = file_match.group("name")
            content = self._read_workspace_file(name)
            if content:
                return content, None
            return "", (
                f"Не нашёл файл «{name}» в рабочей папке. Проверьте имя файла "
                f"или пришлите текст прямо: «озвучь: <текст>»."
            )
        return tail, None

    async def _maybe_answer_voiced_text_retrieval(
        self, text: str, session_id: str
    ) -> BrainResponse | None:
        """Answer "покажи текст, который ты озвучивал" with plain text.

        Returns ``None`` when the request is not a retrieval request.
        """
        stripped = (text or "").strip()
        if not stripped:
            return None
        if not (
            _RETRIEVE_VOICED_RE.search(stripped)
            or _RETRIEVE_SHOW_RE.match(stripped)
        ):
            return None
        voiced = (self._last_voiced_text.get(session_id) or "").strip()
        if not voiced:
            prior = await self._last_assistant_text(session_id)
            voiced = prior.strip()
        if not voiced:
            return BrainResponse(
                text=(
                    "В этой сессии я ещё ничего не озвучивал — сохранённого "
                    "текста нет. Скажите, что озвучить: «озвучь: <текст>»."
                ),
                response_type=ResponseType.CLARIFICATION,
                intent="command.retrieve_text",
            )
        # A pure retrieval/replay is NOT an executed action: claiming a
        # SUCCEEDED tool outcome here made the client render a "✅ Готово"
        # completion header on a plain text answer.  Report a distinct,
        # honest non-action outcome so the header stays reserved for real
        # actions while no failure is implied either.
        return BrainResponse(
            text=voiced,
            response_type=ResponseType.CONVERSATION,
            intent="command.retrieve_text",
            metadata={"tool_outcome": "RETRIEVED"},
        )

    async def _execute_tts_intent(
        self,
        text: str,
        *,
        session_id: str,
        owner_id: str | None = None,
        channel: str = "cli",
        correlation_id: str = "",
        email_to: str = "",
        source_message: str = "",
        explicit_text: str = "",
    ) -> BrainResponse:
        """Resolve a TTS intent to the WORKING ``speech.tts`` contract tool.

        Speech is synthesized in-process by the canonical tool layer (the
        installed ``edge-tts`` CLI with an ffmpeg fallback), producing a real
        Ogg artifact. The ``⟪voice:<path>⟫`` marker in the reply makes the
        Telegram channel deliver it as a voice message; a promised but
        undelivered artifact is reported as a failure, never as done. NO MCP
        server is involved — there is no phantom edge-tts server to call.
        """
        import json as _json

        # Resolve the ACTUAL text to speak.  The inbound message
        # ("озвучь рассказ") is a REQUEST, never the payload: voicing the raw
        # command was the reported defect.  Fail closed with an actionable
        # message when no target text can be resolved.
        if (explicit_text or "").strip():
            # The caller already produced the exact text to speak (combined
            # compose+voice path): never re-derive it from the directive
            # sentence — that would voice a residual fragment.
            spoken, refusal = explicit_text.strip(), None
        else:
            spoken, refusal = await self._resolve_tts_target(
                source_message=source_message or text,
                candidate=text,
                session_id=session_id,
            )
        if refusal is not None:
            return BrainResponse(
                text=refusal,
                response_type=ResponseType.CLARIFICATION,
                intent="task.tts",
            )
        spoken = spoken.strip()
        if not spoken:
            return BrainResponse(
                text=(
                    "Не понял, какой текст озвучить. Напишите: «озвучь: <текст>» "
                    "или используйте /tts <текст>."
                ),
                response_type=ResponseType.CLARIFICATION,
                intent="task.tts",
            )

        from antigona.engine.unified_executor import (
            ToolExecutionRequest,
            UnifiedToolExecutionLayer,
        )
        from antigona.security.auth_service import AuthService

        unified = getattr(self, "_unified_executor", None)
        if not unified:
            unified = UnifiedToolExecutionLayer()
            self._unified_executor = unified

        corr = correlation_id or f"tts-{session_id}"
        request = ToolExecutionRequest(
            tool_name=TTS_CONTRACT_TOOL,
            params={"text": spoken},
            requester="dialogue",
            channel=channel or "cli",
            user_id=owner_id or AuthService().owner_principal_id,
            session_id=session_id,
            correlation_id=corr,
            turn_id=corr,
        )
        try:
            raw = await unified.execute(request)
        except Exception as exc:
            logger.warning("speech.tts execution failed for session=%s: %s", session_id, exc)
            return BrainResponse(
                text=f"Не удалось озвучить текст ({type(exc).__name__}).",
                response_type=ResponseType.ERROR,
                intent="task.tts",
            )

        payload: dict[str, Any] = {}
        if isinstance(raw, str):
            try:
                parsed = _json.loads(raw)
                if isinstance(parsed, dict):
                    payload = parsed
            except Exception:
                payload = {}
        raw_data = payload.get("data")
        data: dict[str, Any] = raw_data if isinstance(raw_data, dict) else {}
        audio_path = str(data.get("audio_path") or "").strip()
        if payload.get("error") or not audio_path:
            reason = str(payload.get("error") or "TTS выполнился без аудиофайла")
            return BrainResponse(
                text=f"Не удалось озвучить: {reason}",
                response_type=ResponseType.ERROR,
                intent="task.tts",
            )

        size = 0
        try:
            from pathlib import Path as _Path

            size = _Path(audio_path).stat().st_size
        except OSError:
            size = 0
        if not size:
            return BrainResponse(
                text="Не удалось озвучить: аудиофайл пуст.",
                response_type=ResponseType.ERROR,
                intent="task.tts",
            )

        email_note = ""
        if email_to:
            try:
                from antigona.tools.registry import _handle_send_email

                eraw = await _handle_send_email(
                    to=email_to,
                    subject="Antigona: озвучка",
                    body=spoken[:500],
                    attachment=audio_path,
                )
                edata = _json.loads(eraw) if isinstance(eraw, str) else {}
                if edata.get("success") or edata.get("ok"):
                    email_note = f"\n📧 Отправил на {email_to}."
                else:
                    email_note = (
                        f"\n⚠️ На почту не ушло: {edata.get('error') or 'неизвестная ошибка'}."
                    )
            except Exception as exc:
                email_note = f"\n⚠️ На почту не ушло ({type(exc).__name__})."

        # Record the voiced text so "покажи текст, который ты озвучивал"
        # returns it verbatim (never a file-write attempt).
        self._last_voiced_text[session_id] = spoken

        # Self-action visibility: the assistant must be able to SEE its own
        # executed action and outcome in later turns, so it can never truthfully
        # claim it did not voice anything.  Mirrors the shell grounding pattern.
        try:
            if self._session_repo is not None:
                await self._session_repo.add_message(
                    session_id=session_id,
                    role="assistant",
                    content=(
                        f"[ACTION] speech.tts — озвучен текст ({len(spoken)} симв., "
                        f"{size} байт, файл {Path(audio_path).name}). Начало текста: "
                        f"{spoken[:200]}"
                    ),
                )
        except Exception:
            logger.debug("tts: action grounding write failed for session=%s", session_id)

        return BrainResponse(
            text=(
                "🎙 Озвучил текст — голосовое сообщение ниже."
                f"{email_note}\n⟪voice:{audio_path}⟫"
            ),
            response_type=ResponseType.CONVERSATION,
            intent="task.tts",
            metadata={"voice_path": audio_path, "size_bytes": size},
        )

    async def _compose_text(self, instruction: str, session_id: str) -> str:
        """Generate the fresh text a combined compose+voice request asks for.

        The composed text is produced through the canonical DialogueEngine
        (the same provider/session/memory path as any conversational turn), so
        the generation step is real and its output — not a fragment of the
        directive — is what the TTS step speaks.  Returns ``""`` when
        generation is impossible; the caller must then fail closed instead of
        voicing something that was never composed.
        """
        engine = self._dialogue_engine
        if engine is None:
            return ""
        try:
            reply = await engine.reply(
                text=instruction, session_id=session_id, context={}
            )
        except Exception:
            logger.exception("compose_text failed for session=%s", session_id)
            return ""
        return reply.strip() if isinstance(reply, str) else ""

    async def _maybe_handle_compose_and_voice(
        self,
        text: str,
        session_id: str,
        *,
        owner_id: str | None = None,
        channel: str = "cli",
        correlation_id: str = "",
    ) -> BrainResponse | None:
        """Router-level composition for "compose X and voice it" messages.

        Live defect: «Напиши короткое стихотворение … и озвучь его голосом» ran
        the TTS branch BEFORE any text existed and spoke the residual fragment
        «его голосом.».  Here the generation step runs FIRST and the TTS step
        consumes exactly the produced text.  Returns ``None`` when the message
        is not a combined compose+voice request.
        """
        if not _is_combined_compose_voice(text):
            return None
        instruction = _compose_instruction(text) or text.strip()
        composed = await self._compose_text(instruction, session_id)
        if not composed:
            # Generation failed: voicing must not run at all (fail closed).
            return BrainResponse(
                text=(
                    "Не удалось сгенерировать текст, поэтому озвучка не "
                    "выполнялась. Попробуйте ещё раз или пришлите текст прямо: "
                    "«озвучь: <текст>»."
                ),
                response_type=ResponseType.CLARIFICATION,
                intent="task.compose_voice",
            )
        voiced = await self._execute_tts_intent(
            composed,
            session_id=session_id,
            owner_id=owner_id,
            channel=channel,
            correlation_id=correlation_id,
            source_message=composed,
            explicit_text=composed,
        )
        if voiced.response_type == ResponseType.ERROR:
            # Text was generated but the audio was not delivered: report BOTH
            # honestly, never as a clean success.
            return BrainResponse(
                text=f"{composed}\n\n{voiced.text}",
                response_type=ResponseType.ERROR,
                intent="task.compose_voice",
            )
        return BrainResponse(
            text=f"{composed}\n\n{voiced.text}",
            response_type=ResponseType.CONVERSATION,
            intent="task.compose_voice",
            metadata=dict(voiced.metadata or {}),
        )

    async def _command_tts(self, text: str) -> BrainResponse:
        """Generate TTS from text and send it to Telegram as a voice message.

        Usage: /tts <текст>. Uses edge-tts (free, ru-RU) to produce an Ogg Opus
        file, then delivers it via TelegramAdapter.send_voice.
        """
        payload = self._strip_cmd(text).strip()
        if not payload:
            return BrainResponse(
                text="Использование: /tts <текст> — озвучить текст голосовым сообщением в Telegram.",
                response_type=ResponseType.CONVERSATION,
                intent="command.tts",
            )
        try:
            import json as _json

            from antigona.tools.registry import _handle_send_voice
            from antigona.voice.tts import text_to_speech
            ogg = await text_to_speech(payload)
            if not ogg:
                return BrainResponse(
                    text="Не удалось синтезировать речь (TTS недоступен).",
                    response_type=ResponseType.ERROR,
                    intent="command.tts",
                )
            sres = await _handle_send_voice(path=ogg, caption=payload[:100])
            sdata = _json.loads(sres)
            delivered = bool(sdata.get("success") or sdata.get("ok"))
            if delivered:
                return BrainResponse(
                    text=f"🎙 Озвучил и отправил голосовое в Telegram:\n`{ogg}`",
                    response_type=ResponseType.CONVERSATION,
                    intent="command.tts",
                    metadata={"voice_path": ogg, "delivered": True},
                )
            return BrainResponse(
                text=f"🎙 Синтез готов, но не доставил в Telegram: {sdata.get('error')}\n`{ogg}`",
                response_type=ResponseType.CONVERSATION,
                intent="command.tts",
            )
        except Exception as exc:
            return BrainResponse(
                text=f"Не удалось озвучить: {exc}",
                response_type=ResponseType.ERROR,
                intent="command.tts",
            )

    async def _command_ollama(self, text: str) -> BrainResponse:
        from antigona.tools.ollama_tool import _handle_ollama
        parts = self._strip_cmd(text).split()
        action = parts[0].lower() if parts else "status"
        model = parts[1] if len(parts) > 1 else ""
        try:
            res = await _handle_ollama(action=action, model=model)
            data = json.loads(res)
            if action in ("status", "start", "list"):
                lines = [f"🤖 Ollama — {action}:"]
                lines.append(f"• serving: {'да' if data.get('serving') else 'нет'}")
                lines.append(f"• модели: {', '.join(data.get('models', [])) or '—'}")
                if data.get("message"):
                    lines.append(f"• {data['message']}")
                return BrainResponse(
                    text="\n".join(lines), response_type=ResponseType.CONVERSATION, intent="command.ollama"
                )
            if action == "switch":
                return BrainResponse(
                    text=str(data.get("message", "Переключено.")),
                    response_type=ResponseType.CONVERSATION,
                    intent="command.ollama",
                )
            return BrainResponse(
                text=json.dumps(data, ensure_ascii=False),
                response_type=ResponseType.CONVERSATION,
                intent="command.ollama",
            )
        except Exception as exc:
            return BrainResponse(
                text=f"ollama: {exc}",
                response_type=ResponseType.CONVERSATION,
                intent="command.ollama",
            )

    async def _command_install(self, text: str, owner_id: str | None = None) -> BrainResponse:
        if self.task_backend is None:
            return BrainResponse(
                text="Gateway недоступен для установки.",
                response_type=ResponseType.ERROR,
                intent="command.install",
            )
        parts = self._strip_cmd(text).split()
        if not parts:
            return BrainResponse(
                text="Использование: /install <pip|npm|apt> <пакет> — установить инструмент в песочницу.",
                response_type=ResponseType.CONVERSATION,
                intent="command.install",
            )
        mgr = parts[0].lower()
        pkg = parts[1] if len(parts) > 1 else ""
        if mgr in ("pip", "pip3", "python"):
            if not pkg:
                return BrainResponse(text="Укажи пакет: /install pip <пакет>.", response_type=ResponseType.CONVERSATION, intent="command.install")
            cmd = ["pip", "install", pkg]
        elif mgr == "npm":
            if not pkg:
                return BrainResponse(text="Укажи пакет: /install npm <пакет>.", response_type=ResponseType.CONVERSATION, intent="command.install")
            cmd = ["npm", "install", "--no-save", pkg]
        elif mgr in ("apt", "apt-get"):
            if not pkg:
                return BrainResponse(text="Укажи пакет: /install apt <пакет>.", response_type=ResponseType.CONVERSATION, intent="command.install")
            cmd = ["apt-get", "install", "-y", pkg]
        else:
            cmd = ["pip", "install", mgr]
        try:
            flow = await self.task_backend.submit_task(
                message=f"Установка инструмента: {' '.join(cmd)}",
                client="cli",
                tool_name="sandbox.shell",
                command=tuple(cmd),
                    owner_id=owner_id or None,
            )
            fid = flow.get("flow_id") or flow.get("id") or "?"
            return BrainResponse(
                text=(
                    f"🔧 Запустил установку `{' '.join(cmd)}` в песочнице.\n"
                    f"Flow: {fid}\nВысокорисковая установка может потребовать /approve."
                ),
                response_type=ResponseType.TASK_ACCEPTED,
                intent="command.install",
                flow_id=str(fid),
            )
        except Exception as exc:
            return BrainResponse(
                text=f"Не удалось запустить установку: {exc}",
                response_type=ResponseType.ERROR,
                intent="command.install",
            )

    @staticmethod
    def _detect_install_plan(base_host: str, rel: str) -> list[tuple[str, list[str]]]:
        """Detect dependency manifests under the host dir *base_host*.

        Returns policy-safe commands using RELATIVE paths (the sandbox workdir is
        /workspace, and is_sensitive_path rejects absolute paths), so the command
        first token is a package manager (pip/npm) — passes the safety gate.
        """
        import os as _os
        plans: list[tuple[str, list[str]]] = []
        if _os.path.exists(_os.path.join(base_host, "requirements.txt")):
            target = f"{rel}/requirements.txt" if rel else "requirements.txt"
            plans.append(("pip", ["pip", "install", "-r", target]))
        if _os.path.exists(_os.path.join(base_host, "pyproject.toml")):
            plans.append(("pip", ["pip", "install", "-e", rel or "."]))
        if _os.path.exists(_os.path.join(base_host, "setup.py")):
            plans.append(("pip", ["pip", "install", "-e", rel or "."]))
        if _os.path.exists(_os.path.join(base_host, "package.json")):
            plans.append(("npm", ["npm", "install", "--prefix", rel or "."]))
        return plans

    async def _command_install_auto(self, text: str, session_id: str, owner_id: str | None = None) -> BrainResponse:
        """Auto-install a project's dependencies by detecting its manifests.

        Usage: /install-auto <relpath-in-workspace>. Detects requirements.txt /
        pyproject.toml / setup.py / package.json and submits a sandbox.shell
        install for each (owner-gated for high-risk). node/go/rust/ruby manifests
        are not auto-submitted because the python-based sandbox lacks those runtimes.
        """
        from antigona.core import paths
        if self.task_backend is None:
            return BrainResponse(
                text="Gateway недоступен для установки.",
                response_type=ResponseType.ERROR,
                intent="command.install_auto",
            )
        ws = paths.workspace_dir()
        rel = self._strip_cmd(text).strip().lstrip("/")
        base = (ws / rel).resolve()
        try:
            base.relative_to(ws)
        except ValueError:
            return BrainResponse(
                text="Путь вне workspace не поддерживается.",
                response_type=ResponseType.ERROR,
                intent="command.install_auto",
            )
        if not base.is_dir():
            return BrainResponse(
                text=f"Проект не найден в workspace: {rel or '.'}",
                response_type=ResponseType.CONVERSATION,
                intent="command.install_auto",
            )
        try:
            sub = str(base.relative_to(ws))
        except ValueError:
            sub = str(base)
        plans = self._detect_install_plan(str(base), sub)
        if not plans:
            return BrainResponse(
                text=f"В проекте {rel or '.'} не найдены манифесты зависимостей "
                     "(requirements.txt, pyproject.toml, setup.py, package.json).",
                response_type=ResponseType.CONVERSATION,
                intent="command.install_auto",
            )
        try:
            lines = [f"🔧 Автоустановка зависимостей проекта «{rel or '.'}»:"]
            submitted = 0
            for label, cmd in plans:
                flow = await self.task_backend.submit_task(
                    message=f"auto-install [{label}]: {' '.join(cmd)}",
                    client="cli",
                    tool_name="sandbox.shell",
                    command=tuple(cmd),
                    owner_id=owner_id or None,
                )
                fid = flow.get("flow_id") or flow.get("id") or "?"
                lines.append(f"• {label}: {' '.join(cmd)} → flow {fid[:8]}")
                submitted += 1
            lines.append(f"Всего задач: {submitted}. Установки идут в песочнице; высокорисковые ждут /approve.")
            return BrainResponse(
                text="\n".join(lines),
                response_type=ResponseType.TASK_ACCEPTED,
                intent="command.install_auto",
            )
        except Exception as exc:
            return BrainResponse(
                text=f"Не удалось запустить автоустановку: {exc}",
                response_type=ResponseType.ERROR,
                intent="command.install_auto",
            )

    async def _command_mcp(self, text: str) -> BrainResponse:
        """Manage registered MCP servers: /mcp list, /mcp add <name> <cmd|url>, /mcp remove <name>.

        Registration is persisted to ``~/.antigona/mcp_servers.json`` via
        ``MCPRegistry`` (same store the ``mcp`` tool uses), so a server added
        here is available to the LLM and every channel on the next turn.

        ``/mcp add <name> <target>`` is classified by scheme: an http(s) target
        is stored as an HTTP server, anything else as a stdio command — never
        by token count, so a URL can't be silently mis-stored as a stdio command.
        """
        from antigona.core.mcp import MCPRegistry

        try:
            args = self._strip_cmd(text).split()
            reg = MCPRegistry.load()
            action = args[0].lower() if args else "list"

            if action == "add" and len(args) >= 2:
                name, target = args[1], args[2] if len(args) >= 3 else ""
                if not name:
                    return BrainResponse(
                        text="Укажи имя сервера: /mcp add <name> <команда|url>.",
                        response_type=ResponseType.CONVERSATION,
                        intent="command.mcp",
                    )
                if not target:
                    return BrainResponse(
                        text="Укажи команду или URL: /mcp add <name> <команда|url>.",
                        response_type=ResponseType.CONVERSATION,
                        intent="command.mcp",
                    )
                if target.startswith(("http://", "https://")):
                    reg.add_http(name, target)
                    reg.save()
                    return BrainResponse(
                        text=f"✅ MCP-сервер «{name}» добавлен (HTTP): {target}",
                        response_type=ResponseType.CONVERSATION,
                        intent="command.mcp",
                    )
                reg.add_stdio(name, target, args[3:])
                reg.save()
                return BrainResponse(
                    text=f"✅ MCP-сервер «{name}» добавлен: {target} {' '.join(args[3:])}",
                    response_type=ResponseType.CONVERSATION,
                    intent="command.mcp",
                )
            if action == "remove" and len(args) == 2:
                removed = reg.remove(args[1])
                if removed:
                    reg.save()
                    return BrainResponse(
                        text=f"✅ MCP-сервер «{args[1]}» удалён.",
                        response_type=ResponseType.CONVERSATION,
                        intent="command.mcp",
                    )
                return BrainResponse(
                    text=f"❌ MCP-сервер «{args[1]}» не найден.",
                    response_type=ResponseType.CONVERSATION,
                    intent="command.mcp",
                )

            # Unknown subcommand with an arg → honest message, not silent list.
            if action not in ("list", ""):
                return BrainResponse(
                    text="Использование: /mcp list, /mcp add <name> <команда|url>, /mcp remove <name>.",
                    response_type=ResponseType.CONVERSATION,
                    intent="command.mcp",
                )

            names = reg.names()
            if not names:
                return BrainResponse(
                    text="📭 Зарегистрированных MCP-серверов нет.\n"
                         "Использование: /mcp add <name> <команда|url> — добавить, /mcp remove <name> — удалить.",
                    response_type=ResponseType.CONVERSATION,
                    intent="command.mcp",
                )
            lines = ["🗂 MCP-серверы:"] + [f"  • {n}" for n in names]
            lines.append("Использование: /mcp add <name> <команда|url>, /mcp remove <name>.")
            return BrainResponse(
                text="\n".join(lines),
                response_type=ResponseType.CONVERSATION,
                intent="command.mcp",
            )
        except Exception as exc:
            logger.warning("mcp command failed: %s", exc)
            return BrainResponse(
                text="Ошибка MCP: операция не выполнена.",
                response_type=ResponseType.ERROR,
                intent="command.mcp",
            )

    async def _command_plugins(self, text: str) -> BrainResponse:
        """Manage plugins: /plugins list, /plugins load <name>, /plugins unload <name>.

        Plugins are discovered under ``~/.antigona/plugins/`` and managed by
        the canonical ``PluginRegistry``/``PluginLoader``. Loading registers
        the plugin's tools/hooks/commands at runtime; unloading cleans them up.

        Security: a plugin name must be a single path-safe token (no ``/``,
        no ``..``, no absolute path) and is resolved strictly inside the
        auto-load root, so ``/plugins load`` can never reach a directory
        outside the plugins tree (no path traversal / arbitrary init.py exec).
        """
        import re

        from antigona.core.paths import owner_dir
        from antigona.plugins import PluginLoader, PluginRegistry

        # Shared, process-live registry + loader (never a per-call throwaway).
        if self._plugin_registry is None or self._plugin_loader is None:
            new_reg = PluginRegistry()
            self._plugin_registry = new_reg
            self._plugin_loader = PluginLoader(new_reg)
        registry: PluginRegistry = self._plugin_registry
        loader: PluginLoader = self._plugin_loader

        _PLUGIN_NAME = re.compile(r"^[\w.-]{1,64}$")

        try:
            args = self._strip_cmd(text).split()
            action = args[0].lower() if args else "list"

            if action == "load" and len(args) == 2:
                name = args[1]
                if not _PLUGIN_NAME.fullmatch(name):
                    return BrainResponse(
                        text=f"❌ Недопустимое имя плагина «{name}» (разрешены буквы/цифры/._-).",
                        response_type=ResponseType.CONVERSATION,
                        intent="command.plugins",
                    )
                plugins_root = owner_dir() / "plugins"
                plugin_dir = (plugins_root / name).resolve()
                # Strict containment: resolved path must stay under the root.
                try:
                    plugin_dir.relative_to(plugins_root.resolve())
                except ValueError:
                    return BrainResponse(
                        text=f"❌ Путь плагина «{name}» вне каталога плагинов.",
                        response_type=ResponseType.CONVERSATION,
                        intent="command.plugins",
                    )
                # An explicit load is also an explicit re-enable.
                loader.enable(name)
                plugin = loader.load_plugin(plugin_dir) if plugin_dir.is_dir() else None
                if plugin is not None:
                    return BrainResponse(
                        text=f"✅ Плагин «{plugin.name}» загружен.",
                        response_type=ResponseType.CONVERSATION,
                        intent="command.plugins",
                    )
                return BrainResponse(
                    text=f"❌ Плагин «{name}» не найден или не загрузился.",
                    response_type=ResponseType.CONVERSATION,
                    intent="command.plugins",
                )
            if action == "unload" and len(args) == 2:
                removed = registry.unregister(args[1])
                if removed:
                    # Durable: auto-discovery must not resurrect it on the next
                    # /plugins listing (PLUG-RELOAD-01).
                    loader.disable(args[1])
                    return BrainResponse(
                        text=f"✅ Плагин «{args[1]}» выгружен.",
                        response_type=ResponseType.CONVERSATION,
                        intent="command.plugins",
                    )
                return BrainResponse(
                    text=f"❌ Плагин «{args[1]}» не найден.",
                    response_type=ResponseType.CONVERSATION,
                    intent="command.plugins",
                )

            # Unknown subcommand with an arg → honest message, not silent list.
            if action not in ("list", ""):
                return BrainResponse(
                    text="Использование: /plugins list, /plugins load <name>, /plugins unload <name>.",
                    response_type=ResponseType.CONVERSATION,
                    intent="command.plugins",
                )

            loader.load_all()
            plugins = registry.list()
            if not plugins:
                return BrainResponse(
                    text="📭 Плагинов не найдено.\n"
                         "Использование: /plugins list, /plugins load <name>, /plugins unload <name>.",
                    response_type=ResponseType.CONVERSATION,
                    intent="command.plugins",
                )
            lines = ["🧩 Плагины:"] + [f"  • {p.name} (v{p.version})" for p in plugins]
            lines.append("Использование: /plugins load <name>, /plugins unload <name>.")
            return BrainResponse(
                text="\n".join(lines),
                response_type=ResponseType.CONVERSATION,
                intent="command.plugins",
            )
        except Exception as exc:
            logger.warning("plugins command failed: %s", exc)
            return BrainResponse(
                text="Ошибка плагинов: операция не выполнена.",
                response_type=ResponseType.ERROR,
                intent="command.plugins",
            )

    async def _command_skills(self, text: str) -> BrainResponse:
        """List installed skills: /skills list."""
        try:
            from antigona.core.paths import skills_dir

            root = skills_dir()
            if not root.is_dir():
                return BrainResponse(
                    text="📭 Навыков не найдено. Директория навыков отсутствует.",
                    response_type=ResponseType.CONVERSATION,
                    intent="command.skills",
                )
            names = sorted(
                p.name for p in root.iterdir()
                if p.is_dir() and (p / "SKILL.md").is_file()
            )
            if not names:
                return BrainResponse(
                    text="📭 Навыков не найдено.",
                    response_type=ResponseType.CONVERSATION,
                    intent="command.skills",
                )
            lines = ["🛠 Навыки:"] + [f"  • {n}" for n in names]
            return BrainResponse(
                text="\n".join(lines),
                response_type=ResponseType.CONVERSATION,
                intent="command.skills",
            )
        except Exception as exc:
            logger.warning("skills command failed: %s", exc)
            return BrainResponse(
                text="Ошибка навыков: операция не выполнена.",
                response_type=ResponseType.ERROR,
                intent="command.skills",
            )

    def _command_cli(self) -> BrainResponse:
        """Show a concise CLI-command reference: /cli — справка по CLI-командам."""
        from antigona.core.command_registry import commands_for_channel

        try:
            specs = commands_for_channel("cli")
            lines = ["🖥 CLI-команды Antigona:"]
            if specs:
                lines += [f"  /{sp.name} — {sp.description}" for sp in specs]
            else:
                lines += [
                    "  /install <pip|npm|apt> <пакет> — установить инструмент в песочницу",
                    "  /mcp list|add|remove — управление MCP-серверами",
                    "  /plugins list|load|unload — управление плагинами",
                    "  /help — все команды",
                ]
            return BrainResponse(
                text="\n".join(lines),
                response_type=ResponseType.CONVERSATION,
                intent="command.cli",
            )
        except Exception:
            return BrainResponse(
                text="🖥 CLI-команды Antigona:\n"
                     "  /install <pip|npm|apt> <пакет>\n"
                     "  /mcp list|add|remove\n"
                     "  /plugins list|load|unload\n"
                     "  /help — все команды",
                response_type=ResponseType.CONVERSATION,
                intent="command.cli",
            )

    async def _command_hermes(self, text: str, session_id: str) -> BrainResponse:
        """Hermes RCA diagnostic commands: /hermes last, /hermes trace <correlation_id>.

        Read-only diagnostic overlay. Lists the most recent RCA verdicts or
        reconstructs the full USER TURN -> ... -> RCA -> REMEDIATION chain for a
        correlation_id. Never mutates anything.
        """
        from antigona.rca.cli import (
            format_evidence,
            format_explain,
            format_result_text,
            format_suggest_fix,
        )
        from antigona.rca.storage import get_repository

        args = self._strip_cmd(text).split()
        action = args[0].lower() if args else "last"
        try:
            repo = get_repository(db_path=self._db_path)
            if action == "trace" and len(args) >= 2:
                cid = args[1]
                rows = repo.by_correlation(cid, limit=50)
                if not rows:
                    return BrainResponse(
                        text=f"🔍 Hermes RCA: по correlation_id «{cid}» записей не найдено.",
                        response_type=ResponseType.CONVERSATION,
                        intent="command.hermes",
                    )
                lines = [f"🔗 Трасса correlation_id={cid} ({len(rows)} записей):"]
                for r in rows:
                    lines.append("  - " + format_result_text(_row_to_dict(r)))
                return BrainResponse(
                    text="\n".join(lines),
                    response_type=ResponseType.CONVERSATION,
                    intent="command.hermes",
                )
            if action == "explain" and len(args) >= 2:
                eid = args[1]
                row = repo.by_error_id(eid)
                if row is None:
                    return BrainResponse(
                        text=f"🔍 Hermes RCA: ошибка «{eid}» не найдена.",
                        response_type=ResponseType.CONVERSATION,
                        intent="command.hermes",
                    )
                return BrainResponse(
                    text=format_explain(_row_to_dict(row)),
                    response_type=ResponseType.CONVERSATION,
                    intent="command.hermes",
                )
            if action == "evidence" and len(args) >= 2:
                rid = args[1]
                row = repo.by_rca_id(rid)
                if row is None:
                    return BrainResponse(
                        text=f"🔍 Hermes RCA: вердикт «{rid}» не найден.",
                        response_type=ResponseType.CONVERSATION,
                        intent="command.hermes",
                    )
                return BrainResponse(
                    text=format_evidence(_row_to_dict(row)),
                    response_type=ResponseType.CONVERSATION,
                    intent="command.hermes",
                )
            if action in ("suggest-fix", "suggest") and len(args) >= 2:
                rid = args[1]
                row = repo.by_rca_id(rid)
                if row is None:
                    return BrainResponse(
                        text=f"🔍 Hermes RCA: вердикт «{rid}» не найден.",
                        response_type=ResponseType.CONVERSATION,
                        intent="command.hermes",
                    )
                return BrainResponse(
                    text=format_suggest_fix(_row_to_dict(row)),
                    response_type=ResponseType.CONVERSATION,
                    intent="command.hermes",
                )
            rows = repo.latest(limit=5)
            if not rows:
                return BrainResponse(
                    text="🤖 Hermes RCA: диагностированных ошибок пока нет.",
                    response_type=ResponseType.CONVERSATION,
                    intent="command.hermes",
                )
            lines = ["🤖 Hermes RCA — последние диагнозы:"]
            for r in rows:
                lines.append(format_result_text(_row_to_dict(r)))
                lines.append("-" * 40)
            return BrainResponse(
                text="\n".join(lines),
                response_type=ResponseType.CONVERSATION,
                intent="command.hermes",
            )
        except Exception as exc:
            return BrainResponse(
                text=f"🤖 Hermes RCA недоступен: {exc}",
                response_type=ResponseType.ERROR,
                intent="command.hermes",
            )

    async def _command_get(self, text: str, session_id: str) -> BrainResponse:
        fid = self._strip_cmd(text).split()[0] if self._strip_cmd(text) else ""
        if not fid:
            fid = self._active_flows.get(session_id) or ""
        if not fid:
            return BrainResponse(
                text="Использование: /get <flow_id>",
                response_type=ResponseType.CONVERSATION,
                intent="command.get",
            )
        if self.task_backend is None:
            return BrainResponse(text="Gateway недоступен.", response_type=ResponseType.ERROR, intent="command.get")
        try:
            view = await self.task_backend.get_flow(fid)
            status = getattr(view, "status", "unknown")
            return BrainResponse(
                text=f"Задача {fid}: {status}",
                response_type=ResponseType.CONVERSATION,
                intent="command.get",
                flow_id=fid,
            )
        except Exception as exc:
            return BrainResponse(
                text=f"Задача {fid} не найдена ({exc})",
                response_type=ResponseType.CONVERSATION,
                intent="command.get",
            )

    async def _command_steer(self, text: str, session_id: str) -> BrainResponse:
        parts = self._strip_cmd(text).split()
        if not parts:
            return BrainResponse(
                text="Использование: /steer <flow_id> <корректировка>",
                response_type=ResponseType.CONVERSATION,
                intent="command.steer",
            )
        fid = parts[0]
        steer_msg = " ".join(parts[1:])
        if not steer_msg:
            return BrainResponse(
                text="Укажи корректировку: /steer <flow_id> <что изменить>",
                response_type=ResponseType.CONVERSATION,
                intent="command.steer",
            )
        if self.task_backend is None:
            return BrainResponse(text="Gateway недоступен.", response_type=ResponseType.ERROR, intent="command.steer")
        try:
            await self.task_backend.steer_flow(fid, steer_msg)
            return BrainResponse(
                text=f"✏️ Скорректировал задачу {fid}.",
                response_type=ResponseType.CONTROL,
                intent="command.steer",
                flow_id=fid,
            )
        except Exception as exc:
            return BrainResponse(
                text=f"Не удалось скорректировать {fid}: {exc}",
                response_type=ResponseType.ERROR,
                intent="command.steer",
            )

    async def _command_list(self) -> BrainResponse:
        return BrainResponse(
            text="Использование: /status (все задачи) или /get <flow_id> (конкретная).\n"
                 "Одобрения: /approvals.",
            response_type=ResponseType.CONVERSATION,
            intent="command.list",
        )

    async def _command_approvals(self, text: str) -> BrainResponse:
        return BrainResponse(
            text="Использование: /approvals — список ожидающих одобрений. /approve <id> — одобрить, /deny <id> — отклонить.",
            response_type=ResponseType.CONVERSATION,
            intent="command.approvals",
        )



    async def _draft_file_content(
        self, text: str, session_id: str
    ) -> tuple[str | None, str]:
        """Черновик содержимого файла + статус (см. ``FileContentDraft``).

        Возвращает ``(content, status)``. Движки без нового API (моки в
        тестах, сторонние реализации) деградируют к ``draft_file_content()``
        со статусом ``DRAFT_UNAVAILABLE`` — прежнее поведение.
        """
        from antigona.conversation.dialogue_engine import DRAFT_OK, DRAFT_UNAVAILABLE

        engine = self._dialogue_engine
        with_status = getattr(engine, "draft_file_content_result", None)
        try:
            if with_status is not None:
                draft = await with_status(text, session_id)
                return draft.content, draft.status
            content = await engine.draft_file_content(text, session_id)
        except Exception:
            logger.exception("draft_file_content failed for session=%s", session_id)
            return None, DRAFT_UNAVAILABLE
        return content, (DRAFT_OK if content else DRAFT_UNAVAILABLE)

    async def _handle_task(
        self,
        text: str,
        session_id: str,
        intent: IntentDecision,
        owner_id: str | None = None,
        correlation_id: str | None = None,
        channel: str = "cli",
    ) -> BrainResponse:
        """Отправить задачу на выполнение через внутренний task_backend.

        Fail-closed: без task_backend задача НЕ создаётся и НЕ выполняется —
        клиент получает честную ошибку, а не локальный исполнитель.

        Args:
            owner_id: Владелец запроса (передаётся явно из process(), никогда
                не хранится на общем singleton — защита от cross-request races).
            correlation_id: Сквозной ID корреляции запроса.
        """
        if self.task_backend is None:
            return BrainResponse(
                text=(
                    "Gateway недоступен. Задача не может быть выполнена. "
                    "Убедитесь, что Gateway запущен (antigona-gateway)."
                ),
                response_type=ResponseType.ERROR,
                intent=intent.intent,
            )

        # «Install <известный пакет>» (в любой форме: ascii/кириллица) —
        # осмысленный ответ вместо задачи, которая ушла бы в песочницу
        # и упёрлась в таймаут.
        known_install = _known_install_reply((), text)
        if known_install is not None:
            return BrainResponse(
                text=known_install,
                response_type=ResponseType.CONVERSATION,
                intent=intent.intent,
            )

        # FP-L05b (live defect 2026-09-18T01:07Z): the tool for a free-text
        # request is decided by the CANONICAL resolver — the same
        # ``resolve_free_text_request`` POST /tasks and FlowEnginePlanner.plan
        # use — not by this module's local heuristic. The heuristic returned
        # ``None`` for "запусти в оболочке команду ls и покажи её вывод", so
        # ``tool_name`` stayed unset, the request fell through to the default
        # write path and the flow was created as ``workspace.write_text`` with
        # the model's drafted answer as the file body. The verifier's plan
        # correctly required ``sandbox.shell``, so the owner got a refusal
        # ("executed tool … does not implement the goal's plan tool") instead
        # of the output of ``ls``.
        canonical_request = resolve_free_text_request(text, decision=intent)
        tool_name: str | None = None
        command: tuple[str, ...] = ()
        if canonical_request.tool_name == "sandbox.shell" and canonical_request.intent == "shell":
            tool_name = "sandbox.shell"
            command = canonical_request.command
        elif intent.intent in ("task.shell", "ambiguous.mixed_intent"):
            extracted = _extract_shell_command(text)
            if extracted:
                tool_name = "sandbox.shell"
                command = (extracted,)

        # MCP requests ("озвучь через mcp: server=..., tool=...") are routed as
        # mcp tasks: the Orchestrator connects to the registered server and calls
        # the remote tool. Requires explicit server+tool in the request text.
        mcp_server: str = ""
        mcp_tool: str = ""
        mcp_arguments: dict[str, Any] | None = None
        email_params: dict[str, Any] | None = None
        tts_params: dict[str, Any] | None = None
        if tool_name is None:
            mcp_request = _parse_mcp_request(text)
            if mcp_request is not None and mcp_request.get("kind") == "tts":
                # Single working TTS path: resolve to the speech.tts contract
                # tool. There is no edge-tts MCP server in this deployment, so
                # nothing may advertise or route to one (no phantom capacity).
                tts_params = dict(mcp_request.get("arguments") or {})
            elif mcp_request is not None:
                mcp_server = str(mcp_request["server"])
                mcp_tool = str(mcp_request["tool"])
                mcp_arguments = mcp_request["arguments"]
                # FAIL FAST: validate registration BEFORE a durable flow is
                # created. An unregistered server gets an actionable rejection
                # listing the known servers instead of a doomed flow that can
                # only end in "Задача не выполнена.".
                rejection = _validate_mcp_server(mcp_server)
                if rejection is not None:
                    return BrainResponse(
                        text=rejection,
                        response_type=ResponseType.ERROR,
                        intent=intent.intent,
                    )
                tool_name = "mcp"
            else:
                email_request = _parse_email_request(text)
                if email_request is not None:
                    tool_name = "send_email"
                    email_params = email_request
                    if not email_params.get("attachment"):
                        email_params["attachment"] = await self._latest_artifact_path(session_id)

        if (
            intent.intent == "task.mcp"
            and tts_params is None
            and tool_name != "mcp"
        ):
            # The router saw a TTS/MCP mention, but it is not an executable
            # request (meta / negated / question). Never fall through to the
            # default write path with the raw text.
            return BrainResponse(
                text=(
                    "Похоже, это вопрос или упоминание об озвучке, а не запрос на "
                    "выполнение. Если нужно озвучить текст — напишите: «озвучь <текст>»."
                ),
                response_type=ResponseType.CONVERSATION,
                intent=intent.intent,
            )

        if tts_params is not None:
            return await self._execute_tts_intent(
                str(tts_params.get("text") or ""),
                session_id=session_id,
                owner_id=owner_id,
                channel=channel,
                correlation_id=correlation_id or "",
                email_to=str(tts_params.get("to") or ""),
                source_message=text,
            )

        # FP-L05b, fail-closed half: an action request whose effect the canonical
        # resolver could NOT resolve is not a file write of its own text. Before
        # this guard the request fell through to the default write path, the LLM
        # drafted an answer, and the flow claimed a completed "side effect" that
        # implemented no plan. Create no flow at all.
        if (
            tool_name is None
            and canonical_request.answer_only
            and intent.intent in ("task.shell", "ambiguous.mixed_intent")
        ):
            return BrainResponse(
                text=(
                    "Не удалось разобрать команду для оболочки. "
                    "Напиши её точнее, например: «выполни ls -la»."
                ),
                response_type=ResponseType.CLARIFICATION,
                intent=intent.intent,
            )

        # Predict whether this shell command will actually need a manual
        # /approve before it can run. The real gate is decided later, async,
        # by the Orchestrator (repository.request_approval) — this is only a
        # best-effort forecast so the user is told upfront instead of being
        # left staring at a silent "принято и выполняется" while the task
        # is actually parked in WAITING_APPROVAL.
        predicted_requires_approval = False
        predicted_risk_reason = ""
        if tool_name == "sandbox.shell" and command:
            from antigona.worker.hitl import evaluate_risk, get_confirmation_policy

            risk_level, predicted_risk_reason = evaluate_risk(
                "sandbox.shell", {"command": list(command)}
            )
            predicted_requires_approval = get_confirmation_policy().should_require_approval(
                risk_level
            )

        # Default path is workspace.write_text: draft the actual file content
        # through the LLM instead of letting the backend fall back to the raw
        # instruction text as content.
        #
        # Контракт (LOOP3, DEFECT 3):
        #   * черновик получен → он и есть содержимое файла;
        #   * DRAFT_REJECTED (провайдер ответил, но выдал пустоту или вопрос)
        #     → fail-closed: задача НЕ создаётся, владелец получает
        #     clarification. Записать инструкцию владельца в файл — заведомо
        #     неверный артефакт, verifier его всё равно завернёт;
        #   * DRAFT_UNAVAILABLE (провайдера нет / он недоступен, degraded или
        #     offline) → прежний fallback: task_service использует `message`
        #     как содержимое, иначе offline-окружение вообще не сможет
        #     создавать задачи.
        read_after_write = False
        run_after_write = False
        fix_after_run = False
        fix_content = ""
        run_command: tuple[str, ...] = ()
        fix_command: tuple[str, ...] = ()
        content: str | None = None
        if tool_name is None:
            plan = parse_goal(text)
            if plan.intent == "file_write_fix_run" and plan.command:
                run_after_write = True
                fix_after_run = True
                run_command = tuple(plan.command.split())
                fix_command = tuple((plan.fix_command or plan.command).split())
                if plan.content:
                    content = strip_code_fences(plan.content, path=plan.path)
                    draft_status = "explicit"
                else:
                    content, draft_status = await self._draft_file_content(text, session_id)
                    if content is not None:
                        content = strip_code_fences(content, path=plan.path)
                if plan.fix_content:
                    fix_content = strip_code_fences(plan.fix_content, path=plan.path)
                else:
                    fix_prompt = (
                        f"Исправь ошибку в коде файла {plan.path} согласно требованию:\n"
                        f"{plan.content_hint or text}\n\n"
                        f"Исходный код:\n{content or ''}\n\n"
                        f"Выведи только исправленный код."
                    )
                    drafted_fix, _ = await self._draft_file_content(fix_prompt, session_id)
                    if not drafted_fix:
                        drafted_fix, _ = await self._draft_file_content(text, session_id)
                    if drafted_fix is not None:
                        fix_content = strip_code_fences(drafted_fix, path=plan.path)
                    else:
                        fix_content = ""
            elif plan.intent == "file_write_run" and plan.command:
                # B5: «создай square.py из описания → запусти python square.py 12».
                # Файл пишется по НАЗВАННОМУ пути (не task_output.txt), исходник
                # генерирует LLM из описания, запуск — отдельный sandbox.shell
                # шаг. plan.content_hint — описание, а НЕ тело файла.
                run_after_write = True
                run_command = tuple(plan.command.split())
                content, draft_status = await self._draft_file_content(text, session_id)
                if content is not None:
                    content = strip_code_fences(content, path=plan.path)
            elif plan.intent == "multi_file" and plan.command:
                tool_name = "sandbox.shell"
                command = (plan.command,)
                content = plan.content
            elif plan.content and not plan.content.rstrip().endswith(":"):
                content = plan.content
                draft_status = "explicit"
            elif (
                plan.content
                and "ровно" in text.casefold()
                and requires_exact_write_read_contract(text, content=plan.content)
            ):
                content = plan.content
                draft_status = "explicit"
            else:
                content, draft_status = await self._draft_file_content(text, session_id)
            from antigona.conversation.dialogue_engine import DRAFT_REJECTED

            if content is None and draft_status == DRAFT_REJECTED:
                logger.warning(
                    "content draft rejected (empty or clarifying question); "
                    "refusing to write the instruction text as file content "
                    "(session=%s)",
                    session_id,
                )
                return BrainResponse(
                    text=(
                        "Не удалось определить содержимое файла. "
                        "Уточни, что именно записать в файл — и я выполню задачу."
                    ),
                    response_type=ResponseType.CLARIFICATION,
                    intent=intent.intent,
                )
            if fix_after_run and not (fix_content and fix_content.strip()):
                logger.warning(
                    "fix content draft rejected (empty or unavailable); "
                    "refusing to create fix step with empty content (session=%s)",
                    session_id,
                )
                return BrainResponse(
                    text=(
                        "Не удалось сформировать исправленный вариант кода. "
                        "Уточни, как именно исправить ошибку — и я выполню задачу."
                    ),
                    response_type=ResponseType.CLARIFICATION,
                    intent=intent.intent,
                )
            read_after_write = not run_after_write and (
                _has_read_back_intent(text)
                or requires_exact_write_read_contract(text, content=content)
            )

        try:
            # Phase 5 / 3.2: explicit task-parameter contract. The intent router
            # extracts structured entities (path, command, ...); they must reach
            # the task backend instead of being dropped. Otherwise a file-write
            # request ("Create xxxx.md") would silently default the target path.
            intent_path = str((intent.entities or {}).get("path") or "").strip()
            if not intent_path:
                try:
                    parsed_path = parse_goal(text).path
                except Exception:
                    parsed_path = ""
                intent_path = str(parsed_path or "").strip()
            submit_kwargs: dict[str, Any] = {
                "message": text,
                "conversation_id": session_id,
                "client": "core",
                "owner_id": owner_id,
                "correlation_id": correlation_id,
                "tool_name": tool_name,
                "command": command,
                "path": intent_path or None,
                "content": content,
                "read_after_write": read_after_write,
                "mcp_server": mcp_server,
                "mcp_tool": mcp_tool,
                "mcp_arguments": mcp_arguments,
                "params": email_params,
            }
            if fix_after_run:
                submit_kwargs["fix_after_run"] = True
                submit_kwargs["fix_content"] = fix_content
                submit_kwargs["fix_command"] = fix_command
                submit_kwargs["run_after_write"] = True
                submit_kwargs["run_command"] = run_command
            elif run_after_write and run_command:
                submit_kwargs["run_after_write"] = True
                submit_kwargs["run_command"] = run_command
            try:
                result = await self.task_backend.submit_task(**submit_kwargs)
            except TypeError:
                # Backends without the compound contract (test doubles, third
                # party implementations) degrade to a plain submit.
                submit_kwargs.pop("fix_after_run", None)
                submit_kwargs.pop("fix_content", None)
                submit_kwargs.pop("fix_command", None)
                had_run = submit_kwargs.pop("run_after_write", None)
                submit_kwargs.pop("run_command", None)
                if had_run:
                    result = await self.task_backend.submit_task(**submit_kwargs)
                elif not read_after_write:
                    submit_kwargs.pop("read_after_write", None)
                    result = await self.task_backend.submit_task(**submit_kwargs)
                else:
                    raise

            # Extract flow_id from result
            flow_id: str | None = None
            if isinstance(result, dict):
                flow_id = str(result.get("flow_id") or result.get("id") or "")
            else:
                flow_id = str(getattr(result, "flow_id", None) or getattr(result, "id", None) or "")

            if flow_id:
                self._active_flows[session_id] = flow_id

            # LOOP4 / DEFECT 2: запомнить цель записи, чтобы следующий ход
            # «прочитай этот же файл обратно» знал, ЧТО читать.
            if tool_name is None and intent_path:
                self._last_write_paths[session_id] = intent_path

            requires_approval = predicted_requires_approval
            if isinstance(result, dict):
                requires_approval = requires_approval or bool(
                    result.get("requires_approval") or False
                )

            if requires_approval:
                text_out = (
                    "Задача принята. Команда требует подтверждения "
                    f"({predicted_risk_reason or 'повышенный риск'}) — "
                    "пришлю запрос на одобрение, отправьте /approve, когда он придёт."
                )
            else:
                text_out = "Задача принята и выполняется."

            return BrainResponse(
                text=text_out,
                response_type=ResponseType.TASK_ACCEPTED,
                flow_id=flow_id or None,
                intent=intent.intent,
                requires_approval=requires_approval,
                metadata={"result": result},
            )
        except Exception as exc:
            from antigona.repository import SensitiveTaskInput

            if isinstance(exc, SensitiveTaskInput):
                # A policy refusal is a MESSAGE, not an internal failure.  The
                # owner must learn that the request was refused (and roughly
                # why), instead of the opaque «не удалось отправить задачу»,
                # which is indistinguishable from a broken backend (FP-L03d).
                logger.warning(
                    "task submit refused by safety policy for session=%s",
                    session_id,
                )
                return BrainResponse(
                    text=(
                        "🚫 Команда не отправлена: её отклонила защита. "
                        "Абсолютные host-пути и секретные файлы в командах "
                        "запрещены — напиши команду в пределах рабочей папки, "
                        "например: «выполни ls -la» или «выполни ls *.txt»."
                    ),
                    response_type=ResponseType.CLARIFICATION,
                    intent=intent.intent,
                )
            logger.warning(
                "task_backend submit failed for session=%s: %s",
                session_id,
                exc,
            )
            return BrainResponse(
                text="Не удалось отправить задачу на выполнение.",
                response_type=ResponseType.ERROR,
                intent=intent.intent,
            )

    # ── Session management ────────────────────────────────────────────────────

    async def _ensure_session(
        self,
        session_id: str,
        channel: str = "",
        user_id: str = "",
    ) -> None:
        """Убедиться, что сессия существует. Создать если нет."""
        await self.connect()
        try:
            if not await self._session_repo.session_exists(session_id):
                title = f"{channel.capitalize()} session ({user_id})"
                await self._session_repo.create_session(
                    session_id=session_id,
                    title=title,
                )
                logger.info("Created session: %s", session_id)
        except Exception as exc:
            logger.warning("Session management error for %s: %s", session_id, exc)

    # ── Memory summarizer management ──────────────────────────────────────────

    def _get_summarizer(self, session_id: str) -> MemorySummarizer:
        """Получить или создать MemorySummarizer для сессии."""
        if session_id not in self._summarizers:
            self._summarizers[session_id] = MemorySummarizer()
        return self._summarizers[session_id]

    # ── Active flow management ────────────────────────────────────────────────

    def get_active_flow(self, session_id: str) -> str | None:
        """Получить ID активной задачи для сессии."""
        return self._active_flows.get(session_id)

    def set_active_flow(self, session_id: str, flow_id: str | None) -> None:
        """Установить или сбросить активную задачу для сессии."""
        if flow_id:
            self._active_flows[session_id] = flow_id
        else:
            self._active_flows.pop(session_id, None)

    # ── Convenience ───────────────────────────────────────────────────────────

    @property
    def intent_router(self) -> IntentRouter:
        """Доступ к единому IntentRouter."""
        return self._intent_router

    @property
    def dialogue_engine(self) -> DialogueEngine:
        """Доступ к единому DialogueEngine."""
        return self._dialogue_engine

    @property
    def session_repository(self) -> SessionRepository:
        """Доступ к единому SessionRepository."""
        return self._session_repo


def _row_to_dict(row: Any) -> dict[str, Any]:
    """Convert an RCAErrorRecord ORM row into the dict format_result_text expects."""
    import json as _json

    def _loads(s: str) -> Any:
        try:
            return _json.loads(s or "[]")
        except Exception:
            return []

    return {
        "rca_id": row.rca_id,
        "error_id": row.error_id,
        "correlation_id": row.correlation_id,
        "category": row.category,
        "confidence": row.confidence,
        "status": row.status,
        "severity": row.severity,
        "source_component": row.source_component,
        "root_cause": row.root_cause,
        "user_impact": row.user_impact,
        "summary": row.summary,
        "exception_type": row.exception_type,
        "error_message": row.error_message,
        "duplicate_count": row.duplicate_count,
        "tool_name": row.tool_name,
        "provider": row.provider,
        "model": row.model,
        "task_id": row.task_id,
        "flow_id": row.flow_id,
        "step_id": row.step_id,
        "git_revision": row.git_revision,
        "recommended_actions": _loads(row.recommended_actions),
        "evidence": _loads(row.evidence),
        "safe_to_auto_fix": row.safe_to_auto_fix,
        "requires_owner_approval": row.requires_owner_approval,
    }

