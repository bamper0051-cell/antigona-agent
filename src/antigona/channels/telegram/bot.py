"""Thin Telegram handler — no business logic.

Receives messages → routes through command/intent router → responds via transport.
All business logic (conversation, intent routing) lives in dedicated modules.

Event integration:
  - Every incoming message publishes ``MessageReceived``
  - Every classified intent publishes ``IntentClassified``
  - Every reply publishes ``ConversationReply``
  - Cancel commands and callback actions publish ``CancelRequested``

Session persistence (Day 10):
  - ``/start`` creates a new session record in SQLite
  - Every user message is stored via ``SessionRepository.add_message``
  - Every router decision is stored via ``SessionRepository.add_decision``
"""

from __future__ import annotations  # noqa: I001

import asyncio
import html
import logging
import os
import re
import secrets
import sys
import time
from collections import OrderedDict
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from aiogram import F, Router
from aiogram.enums import ChatAction
from aiogram.filters import Command
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, ErrorEvent, Message

# ── InputPipeline ────────────────────────────────────────────────────
# Re-export for backward compatibility with tests ──────────────────────────────
# These must be importable from ``antigona.channels.telegram.bot`` so existing
# tests continue to work without changes.
from antigona.core import paths
from antigona.core.control_plane import FlowStatus

# GatewayClient — единый способ управления задачами через Gateway
from antigona.core.gateway_client import GatewayClient, GatewayFlowNotFoundError
from antigona.channels.telegram.bridge import (
    DEFAULT_MAX_FILE_BYTES,
    TELEGRAM_TEXT_LIMIT,
    AmbiguousTurn,
    AttachmentRejected,
    BridgeOverflow,
    TelegramBridge,
    TurnCancelled,
    TurnIdentity,
    discard_inbound_file,
    ensure_task_tag_in_first_paragraph,
    extract_task_tag,
    finalize_inbound_file,
    prepare_inbound_file,
    sanitize_filename,
    split_telegram_html,
    utf16_length,
)
from antigona.channels.telegram.turn_ledger import (
    TurnLedger,
    ensure_ledger_writable,
)
from antigona.input_pipeline import BindingRepository
from antigona.input_pipeline.models import ProcessingOutcome
from antigona.result_safety import (
    is_usable_result_text,
    public_failure_reason,
    sanitize_result_text,
)
from antigona.router.intent_router import ConversationState
from antigona.voice import speech_to_text

# Re-export для тестов (Step 4: бот не исполняет chitchat локально, но
# контракт модуля сохранён).
from antigona.conversation.engine import (  # noqa: F401
    chitchat_reply,
    is_chitchat_or_noise,
)

# ── FP-L22: single shared truthfulness criterion ───────────────────────────
# The stale-error marker set, the neutral stub and the leak criterion are NOT
# duplicated in this module — they live in ``conversation.dialogue_engine`` and
# are imported here so both guards use one and the same rule.
from antigona.conversation.dialogue_engine import (  # noqa: F401
    _INTERNAL_ERROR_MARKERS as _STALE_ERROR_MARKERS,
    _NEUTRAL_NO_ACTION_REPLY as _NEUTRAL_STALE_ERROR_REPLY,
    _UNGROUNDED_TOOL_BREAKAGE_REPLY,
    _UNGROUNDED_TOOL_OUTPUT_REPLY,
    contradicts_a_successful_tool,
    internal_marker_leak,
    ungrounded_tool_output_claim,
)

from antigona.database import Database

# ── Operation Store & Presenter ────────────────────────────────────────────
from antigona.durable.operation_models import OperationState, StateMachine
from antigona.durable.operation_store import OperationStore

# Event system
from antigona.events.bus import EventBus
from antigona.events.event_types import (
    ConversationReply,
    FinalResponseReady,
    MessageReceived,
    OperationReceived,
    StageChanged,
    ToolProgress,
)

# Health: heartbeat files of non-HTTP services (worker/verifier)
from antigona.health.heartbeat import read_status

# ── New subsystems: Hooks, Voice, Browser ────────────────────────────────
from antigona.hooks import get_hook_registry
from antigona.hooks.hooks import fire_hooks_for_event
from antigona.presentation.presenter import MAX_FINAL_TEXT, OperationPresenter

# Context references (@file, @folder, @url)
from antigona.tools.context_refs import expand_refs

# Authoritative system clock (never a model-invented date)
from antigona.tools.system_time import get_current_system_time

# Transport layer
from antigona.transport.telegram import (
    ApprovalCallback,
    PidLockError,
    TelegramTransport,
    acquire_pid_lock,
    format_card,  # noqa: F401 — re-export для тестов
    is_approval_token,
    make_approval_keyboard,
    resolve_approval_token,
)
logger = logging.getLogger(__name__)

#: Ceiling for a single inbound Telegram attachment.
MAX_ATTACHMENT_BYTES = DEFAULT_MAX_FILE_BYTES

#: How many inbound attachment handles stay resolvable per process.
_MAX_TRACKED_ATTACHMENTS = 256


@dataclass(frozen=True, slots=True)
class _AttachmentRef:
    """One transport-normalised inbound file, whatever Telegram called it."""

    file_id: str
    filename: str
    size: int
    mime_type: str


# ─── Health report ────────────────────────────────────────────────────────────

#: Non-HTTP services reported by ``/health`` (heartbeat-backed, see
#: ``antigona.health.heartbeat``).
_HEALTH_SERVICES: tuple[str, ...] = ("worker", "verifier")


def _describe_service(status: dict[str, object] | None) -> str:
    """Human-readable liveness of one heartbeat-backed service.

    Honest by construction: a service without a heartbeat file is reported as
    unknown, never as ``ok``.
    """
    if status is None:
        return "неизвестно (нет heartbeat)"
    return "ok" if status.get("up") else "down"


def build_health_report(
    *,
    gateway: str,
    services: dict[str, dict[str, object]],
    now_utc: str,
) -> str:
    """Build the structured ``/health`` reply (gateway / worker / verifier / time)."""
    lines = [
        "🩺 Здоровье Antigona:",
        f"• gateway — {gateway}",
    ]
    for name in _HEALTH_SERVICES:
        lines.append(f"• {name} — {_describe_service(services.get(name))}")
    lines.append(f"• время (UTC) — {now_utc}")
    return "\n".join(lines)


def collect_health_report(gateway: str) -> str:
    """Read real service state (heartbeats + system clock) and format it."""
    try:
        services = read_status()
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("heartbeat read failed: %s", exc)
        services = {}
    now_utc = get_current_system_time()["utc_iso"]
    return build_health_report(gateway=gateway, services=services, now_utc=str(now_utc))


# ─── Command handling ─────────────────────────────────────────────────────────

_ALLOWED_HTML_TAGS = frozenset({
    "b", "i", "u", "s", "a", "code", "pre", "strong", "em",
    "ins", "del", "tg-spoiler",
})

def _telegram_safe_html(text: str) -> str:
    """Strip HTML tags that Telegram's parse_mode=HTML doesn't support."""
    return re.sub(
        r"</?(\w+)[^>]*>",
        lambda m: m.group(0) if m.group(1).lower() in _ALLOWED_HTML_TAGS else "",
        text,
    )

def _build_help_text() -> str:
    """Build a formatted /help message from the unified command registry.

    Step 10 (манифест): единый реестр команд — один источник для CLI и
    Telegram. Локальные (UI/транспортные) команды добавляются отдельно.
    """
    from antigona.core.command_registry import commands_for_channel

    lines: list[str] = ["🤖 <b>Antigona Telegram Bot</b>\n"]
    lines.append("━━━ <b>Управление</b> ━━━")
    lines.append("  /start — приветствие и краткая инструкция")
    lines.append("  /help — этот список команд")
    lines.append("  /pin — установить PIN-код")
    lines.append("  /unlock — разблокировать действия")
    lines.append("  /lock — заблокировать действия")
    lines.append("  /auth_status — статус авторизации")
    lines.append("  /confirm &lt;код&gt; — подтвердить операцию")
    lines.append("")
    lines.append("━━━ <b>Задачи (через Gateway)</b> ━━━")
    for spec in commands_for_channel("telegram"):
        args = " ".join(f"&lt;{html.escape(a)}&gt;" for a in spec.arguments) if spec.arguments else ""
        lines.append(f"  /{spec.name} {args} — {html.escape(spec.description)}")
    lines.append("")
    lines.append("━━━ <b>Память (через Gateway)</b> ━━━")
    lines.append("  /memory — показать память агента")
    lines.append("  /remember &lt;текст&gt; — запомнить факт")
    lines.append("  /forget &lt;id&gt; — удалить запись памяти")
    lines.append("")
    lines.append("━━━ <b>Контекст</b> ━━━")
    lines.append("  @file:path — вставить содержимое файла")
    lines.append("  @folder:path — показать дерево папки")
    lines.append("  @url:url — загрузить содержимое URL")
    lines.append("")
    lines.append("💡 <i>Также можно просто описать задачу текстом — "
                 "ядро создаст task flow автоматически.</i>")
    return "\n".join(lines)

class StatusParseOutcome(StrEnum):
    """Outcome of parsing a status command string."""

    NOOP = "NOOP"
    LIST_FLOWS = "LIST_FLOWS"
    GET_FLOW = "GET_FLOW"
    MALFORMED = "MALFORMED"


@dataclass(frozen=True, slots=True)
class ParsedStatusRequest:
    """Parsed result of a status command request."""

    outcome: StatusParseOutcome
    resource_id: str = ""
    error: str = ""


def parse_status_request(text: str) -> ParsedStatusRequest:
    """Parse a status command input for list-flows vs single flow status query.

    Rules:
      - Pure whitespace input (e.g. "", "  \t\n") -> NOOP.
      - Exact "/status" (case-insensitive, no surrounding whitespace) -> LIST_FLOWS.
      - "/status ", "/status  ", "/status\t", "/status\n" (empty or padded ID form) -> MALFORMED (FAIL_CLOSED).
      - "/status <id>" with a non-empty resource ID -> GET_FLOW with resource_id.
      - Any other input not starting with "/status" -> NOOP.
    """
    if not text or not text.strip():
        return ParsedStatusRequest(outcome=StatusParseOutcome.NOOP)

    # Check for exact "/status" (case-insensitive, no surrounding whitespace)
    if text.lower() == "/status":
        return ParsedStatusRequest(outcome=StatusParseOutcome.LIST_FLOWS)

    # Check if text starts with /status (case-insensitive)
    raw = text.lstrip()
    if not raw.lower().startswith("/status"):
        return ParsedStatusRequest(outcome=StatusParseOutcome.NOOP)

    # Extract remainder after /status
    remainder = raw[7:]
    if remainder.startswith("@"):
        parts = remainder.split(maxsplit=1)
        remainder = parts[1] if len(parts) > 1 else ""

    if not remainder:
        # Exact /status with leading whitespace (surrounding whitespace)
        return ParsedStatusRequest(
            outcome=StatusParseOutcome.MALFORMED,
            error="MALFORMED: empty or padded resource-ID value rejected",
        )

    resource_id = remainder.strip()
    if not resource_id:
        # Remainder was pure whitespace (e.g. "/status ", "/status  ", "/status\t", "/status\n")
        return ParsedStatusRequest(
            outcome=StatusParseOutcome.MALFORMED,
            error="MALFORMED: empty or padded resource-ID value rejected",
        )

    # If leading whitespace existed before /status, it's also padded
    if text != text.lstrip():
        return ParsedStatusRequest(
            outcome=StatusParseOutcome.MALFORMED,
            error="MALFORMED: empty or padded resource-ID value rejected",
        )

    return ParsedStatusRequest(
        outcome=StatusParseOutcome.GET_FLOW,
        resource_id=resource_id,
    )


def parse_command(text: str) -> str:
    """Extract a bare command name from a message text.

    '/status@antigona_bot arg' -> 'status'. Returns '' if text is not a command.
    """
    stripped = (text or "").strip()
    if not stripped.startswith("/"):
        return ""
    head = stripped.split(maxsplit=1)[0]
    return head[1:].split("@", 1)[0].lower()


def unknown_command_reply(text: str) -> str:
    """Reply for a slash command the bot does not know.

    Step 10: список команд берётся из единого реестра команд.
    """
    cmd = parse_command(text)
    shown = f"/{cmd}" if cmd else text.strip()
    from antigona.core.command_registry import commands_for_channel

    registry = commands_for_channel("telegram")
    known = [
        "/start", "/help", "/pin", "/unlock", "/lock",
        "/auth_status", "/confirm",
    ] + [f"/{spec.name}" for spec in registry]
    available = ", ".join(known)
    return (
        f"❓ Неизвестная команда: {shown}\n"
        f"Доступные команды: {available}\n"
        "Или просто опишите задачу текстом — я создам task flow."
    )


def _describe_reply_media(message: Message) -> str:
    """Return a short Russian placeholder describing a non-text message.

    Used when a quoted (reply-to) message has no text/caption — e.g. a
    photo, voice note, or document — so the LLM prompt still carries a
    meaningful stand-in instead of an empty string.
    """
    if message.photo:
        return "[Фото]"
    if message.voice:
        return "[Голосовое сообщение]"
    if message.video_note:
        return "[Видео-кружок]"
    if message.video:
        return "[Видео]"
    if message.audio:
        return "[Аудио]"
    if message.document:
        return "[Файл]"
    if message.sticker:
        return "[Стикер]"
    if message.animation:
        return "[GIF]"
    if message.location:
        return "[Геолокация]"
    if message.contact:
        return "[Контакт]"
    if message.poll:
        return "[Опрос]"
    return "[Медиа]"


def _reply_author_name(reply_to: Message) -> str:
    """Resolve a display name for the author of a quoted message.

    Antigona's own messages are attributed to "Антигона" (Telegram bots
    are reported via ``from_user.is_bot``) rather than the bot's raw
    username.
    """
    user = reply_to.from_user
    if user is None:
        return "неизвестный автор"
    if user.is_bot:
        return "Антигона"
    if user.full_name:
        return user.full_name
    if user.username:
        return f"@{user.username}"
    return "пользователь"


def build_reply_context_block(message: Message) -> str | None:
    """Build a ``[Контекст: ...]`` block describing the message being replied to.

    Returns ``None`` when *message* is not a reply, so callers can pass the
    user's text through unwrapped — no extra structure is added to prompts
    for plain (non-reply) messages.
    """
    reply_to = message.reply_to_message
    if reply_to is None:
        return None

    author = _reply_author_name(reply_to)
    quoted_text = (reply_to.text or reply_to.caption or "").strip()
    if not quoted_text:
        quoted_text = _describe_reply_media(reply_to)

    quoted_text = quoted_text.replace("\n", " ")
    if len(quoted_text) > 300:
        quoted_text = quoted_text[:300] + "…"

    return f'[Контекст: Пользователь отвечает на сообщение от {author}: "{quoted_text}"]'


def should_process_message(
    message: Any,
    bot_username: str | None = None,
    bot_id: int | None = None,
) -> bool:
    """Determine whether the message should be processed based on addressing gates.

    Returns:
        bool: True if the message should be processed, False to ignore/silent return.
    """
    # 1. Messages from bots are completely ignored
    from_user = getattr(message, "from_user", None)
    if from_user and getattr(from_user, "is_bot", False):
        return False

    chat = getattr(message, "chat", None)
    chat_type = getattr(chat, "type", "private") if chat else "private"

    # 2. Private chats are fully processed
    if chat_type == "private":
        return True

    # 3. Group/supergroup addressing gates
    if chat_type in ("group", "supergroup"):
        text = getattr(message, "text", "") or ""
        caption = getattr(message, "caption", "") or ""
        text_or_caption = text or caption or ""

        # a) Starts with "/" (command)
        if text_or_caption.startswith("/"):
            return True

        # b) Reply to message from bot
        reply_to = getattr(message, "reply_to_message", None)
        if reply_to:
            reply_to_user = getattr(reply_to, "from_user", None)
            if reply_to_user and getattr(reply_to_user, "is_bot", False):
                return True

        # c) Contains bot mention (mention entity with @username or text_mention with bot_id)
        entities = getattr(message, "entities", None) or []
        caption_entities = getattr(message, "caption_entities", None) or []
        all_entities = list(entities) + list(caption_entities)

        for entity in all_entities:
            entity_type = getattr(entity, "type", "")
            offset = getattr(entity, "offset", 0)
            length = getattr(entity, "length", 0)

            if entity_type == "mention" and bot_username:
                # Extract the mention text
                mention_text = text_or_caption[offset:offset + length]
                if mention_text.lower() == f"@{bot_username.lower()}":
                    return True
            elif entity_type == "text_mention" and bot_id:
                entity_user = getattr(entity, "user", None)
                entity_user_id = getattr(entity_user, "id", None) if entity_user else None
                if entity_user_id == bot_id:
                    return True

    return False


def _generate_correlation_id() -> str:
    """Generate a short unique correlation ID for a message lifecycle."""
    import random
    import string

    ts = int(time.monotonic() * 1000)
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
    return f"corr-{ts:x}-{suffix}"


_FAILED_FLOW_STATUSES = frozenset(
    {
        FlowStatus.FAILED,
        FlowStatus.BLOCKED,
        FlowStatus.TIMEOUT,
        FlowStatus.POLICY_DENIED,
    }
)
_ACTION_CANCELLED_ERROR = "CANCELLED"


def _coerce_processing_outcome(value: object) -> ProcessingOutcome | None:
    if isinstance(value, ProcessingOutcome):
        return value
    try:
        return ProcessingOutcome(str(value).upper())
    except ValueError:
        return None


def _coerce_flow_status(value: object) -> FlowStatus | None:
    if isinstance(value, FlowStatus):
        return value
    try:
        return FlowStatus(str(value).upper())
    except ValueError:
        return None


def _open_operation_state(status: FlowStatus | None) -> OperationState:
    """Map a still-open flow status onto the OperationState that DESCRIBES it.

    The terminal wait has a budget; when it expires the flow keeps running and
    the operation must NOT be reported as a blanket ``RUNNING``.  A flow parked
    on an approval (or on any other owner decision) is waiting for the OWNER,
    which is exactly ``WAITING_USER`` — the presenter can then tell the truth
    («ждёт вашего решения») instead of dropping a nonterminal outcome (FP-L03d).
    """
    if status in (FlowStatus.WAITING_APPROVAL, FlowStatus.WAITING_USER):
        return OperationState.WAITING_USER
    return OperationState.RUNNING


def _processing_terminal_state(result: Any) -> OperationState | None:
    """Map one typed pipeline result to an operation terminal, fail-closed."""
    outcome = _coerce_processing_outcome(getattr(result, "outcome", None))
    status = _coerce_flow_status(getattr(result, "flow_status", None))
    is_terminal = bool(getattr(result, "terminal", False))
    pipeline_succeeded = bool(getattr(result, "success", False))
    error = getattr(result, "error", None)

    # The typed semantic outcome dominates the retained raw Gateway status.
    # InputPipeline intentionally retains DONE when its safe result projection
    # fails and reports TERMINAL_FAILURE.
    if outcome is ProcessingOutcome.TERMINAL_FAILURE:
        return OperationState.FAILED
    if outcome is ProcessingOutcome.CANCELLED:
        return OperationState.CANCELLED
    if outcome is ProcessingOutcome.TERMINAL_SUCCESS:
        safe_result = sanitize_result_text(
            getattr(result, "response_text", None),
            max_length=3500,
        )
        return (
            OperationState.SUCCEEDED
            if pipeline_succeeded
            and not error
            and is_terminal
            and status is FlowStatus.DONE
            and is_usable_result_text(safe_result)
            else OperationState.FAILED
        )

    if not pipeline_succeeded or error:
        return OperationState.FAILED
    if outcome is ProcessingOutcome.CONVERSATION_FINAL:
        return None

    if is_terminal and status is not None:
        if status in _FAILED_FLOW_STATUSES:
            return OperationState.FAILED
        if status is FlowStatus.CANCELLED:
            return OperationState.CANCELLED
        # No raw terminal status, including DONE, may synthesize success
        # without the explicit TERMINAL_SUCCESS contract above.
        return OperationState.FAILED
    return None


def _safe_result_text(
    value: object | None,
    *,
    fallback: str,
    max_length: int = 2000,
) -> str:
    """Bound/redact untrusted producer text before it enters typed events."""
    safe = sanitize_result_text(value, max_length=max_length)
    return safe.strip() if safe and safe.strip() else fallback


#: Internal security wording that must never reach the owner chat verbatim.
#: These markers describe ownership-lease / fencing-token / fail-closed
#: internals, which are meaningless and alarming to the end user.
_INTERNAL_SECURITY_MARKERS: tuple[str, ...] = (
    "fencing token",
    "workspace fence",
    "ownership fence",
    "ownership",
    "fail-closed",
    "fail closed",
    "denied_stale_fence",
    "stale fence",
    "deny-all",
    "write permit",
    "writepermit",
    "owner lease",
    "lease expired",
)


def _failure_text_with_reason(base: str, error: object | None) -> str:
    """Honest terminal failure: append the bounded, sanitized REAL reason.

    Never fabricates success and never leaks raw exception/stack text or
    internal ownership/fencing wording (``public_failure_reason`` refuses the
    former and neutralizes the latter). Without a safe reason the bare base
    text is returned.
    """
    reason = public_failure_reason(error)
    if not reason:
        return base
    return _sanitize_user_facing_error(f"{base} Причина: {reason}")


def _sanitize_user_facing_error(text: str) -> str:
    """Strip internal security wording from a user-visible failure.

    The failure stays honest (the action was refused) and actionable (what to
    do next) without leaking fencing-token / ownership / fail-closed internals.
    """
    low = (text or "").lower()
    if any(marker in low for marker in _INTERNAL_SECURITY_MARKERS):
        return (
            "🚫 Действие отклонено защитой рабочей области: запрошенная "
            "операция выходит за разрешённые границы. Изменений не внесено. "
            "Уточните путь или команду и повторите."
        )
    return text


#: FP-L22: the guard is NOT re-implemented here.  The stale-error marker set,
#: the neutral stub text and the criterion itself live in exactly ONE place —
#: ``antigona.conversation.dialogue_engine`` — and are imported at the top of
#: this module.  The criterion additionally keeps an owner-quoted internal term
#: («что такое fail-closed?») from being mistaken for a leak.


def _contains_stale_error_marker(text: str) -> bool:
    """Marker present with NO owner context (no user message supplied).

    Legacy thin wrapper around the single shared criterion: with an empty user
    message the exemption can never apply, so a present marker is a leak.
    """
    return internal_marker_leak(text)


# ─── Telegram Bot Handler (thin) ──────────────────────────────────────────────


class TelegramBot(TelegramTransport):
    """Telegram Bot handler. Thin — only registers handlers and routes messages.

    Inherits transport infrastructure (Bot, Dispatcher, Gateway HTTP/WS calls,
    circuit breaker, dedup, rate limit) from TelegramTransport.

    Event integration via ``self.event_bus`` (an ``EventBus`` instance).

    Handler flow:
      receive message → publish MessageReceived → parse command → intent router
      → publish IntentClassified → respond → publish ConversationReply
    """

    def __init__(
        self,
        token: str | None = None,
        gateway_url: str = "http://127.0.0.1:8090",
        gateway_token: str = "gateway-token",
        storage: MemoryStorage | None = None,
        event_bus: EventBus | None = None,
        gateway_client: GatewayClient | None = None,
        database_url: str | None = None,
    ) -> None:
        super().__init__(
            token=token,
            gateway_url=gateway_url,
            gateway_token=gateway_token,
            storage=storage,
        )
        from aiogram import Router as AiogramRouter

        self.router: AiogramRouter = Router()
        # Step 4 (манифест): бот — чистый транспорт. Никаких локальных
        # IntentRouter / Planner / TaskRuntime / TaskManager / ContextResolver /
        # файловой памяти / ботовых сессий / провайдеров. Единственный путь —
        # GatewayClient (Gateway Turn API). Ядро маршрутизирует всё.
        # GatewayClient — единый способ управления задачами
        self.gateway_client: GatewayClient = gateway_client or GatewayClient(
            base_url=self.gateway_url,
            token=self.gateway_token,
        )
        # Durable exactly-once ledger: Telegram redelivers updates and this
        # process can restart mid-turn, so the "one agent run per message"
        # promise has to survive both. An explicitly empty path degrades to an
        # in-memory ledger (still exactly-once for the life of the process).
        ledger_path = str(paths.turn_ledger_path())
        self.telegram_bridge = TelegramBridge(
            self.gateway_client,
            ledger=TurnLedger(ledger_path) if ledger_path else None,
        )
        # EventBus for internal presentation events
        self.event_bus = event_bus or EventBus()
        resolved_db_url = database_url or os.getenv(
            "ANTIGONA_DATABASE_URL"
        ) or f"sqlite:///{paths.database_path()}"
        self.database = Database(resolved_db_url)
        # create_all() is idempotent (checkfirst), safe to call unconditionally:
        # OperationStore/BindingRepository need their tables on a fresh DB.
        self.binding_repo = BindingRepository(self.database)
        # Cached bot identity (resolved lazily in _resolve_bot_info)
        self.bot_username: str | None = None
        self.bot_id: int | None = None
        # ── Operation Store & Presenter (транспортная презентация) ──────
        self.operation_store = OperationStore(self.database)
        self.operation_presenter = OperationPresenter(
            self.bot, self.event_bus, self.operation_store,
        )
        # Presenter subscriptions are installed by the dispatcher startup hook.
        self.conversation_states: dict[int, ConversationState] = {}
        # Opaque handles for inbound attachments, so the runtime never has to
        # be handed a raw filesystem path it did not ask for.
        self._attachments: OrderedDict[str, Path] = OrderedDict()
        # Flow-и, для которых карточка одобрения уже показана: не дублируем
        # «⚠️ Требуется одобрение:» каждую секунду поллинга _wait_flow_terminal.
        self._approval_cards_shown: set[str] = set()

        # Wire hooks system into the event bus
        self._hook_registry = get_hook_registry()
        self._event_unsubscribe = self.event_bus.subscribe_any(
            self._on_any_event
        )

        self._register_handlers()
        self.dp.include_router(self.router)

        # ── Startup hooks — registered on aiogram's dispatcher lifecycle ──
        self._startup_recovery_task: asyncio.Task[Any] | None = None
        self._started = False
        self._closed = False
        self.dp.startup.register(self._on_dispatcher_startup)
        self.dp.shutdown.register(self._on_dispatcher_shutdown)

    async def _resolve_bot_info(self) -> tuple[str | None, int | None]:
        """Resolve bot's username and ID and cache them."""
        if not hasattr(self, "bot_username") or self.bot_username is None:
            self.bot_username = None
            try:
                me = await self.bot.me()
                if me:
                    self.bot_username = me.username
            except Exception:
                pass
            if self.bot_username is None:
                try:
                    me = await self.bot.get_me()
                    if me:
                        self.bot_username = me.username
                except Exception:
                    pass

        if not hasattr(self, "bot_id") or self.bot_id is None:
            self.bot_id = None
            try:
                me = await self.bot.me()
                if me:
                    self.bot_id = me.id
            except Exception:
                pass
            if self.bot_id is None:
                try:
                    me = await self.bot.get_me()
                    if me:
                        self.bot_id = me.id
                except Exception:
                    pass

        return self.bot_username, self.bot_id

    # ── Bridge helpers — the single canonical runtime handoff ────────────

    @staticmethod
    def _turn_identity(message: Message, kind: str) -> TurnIdentity:
        """Canonical identity of one Telegram-originated agent turn.

        ``message_id`` is not an identity on its own: an edit reuses the id of
        the message it edits, and ids are only unique within a chat.  Chat,
        forum topic, update kind and edit revision all participate so that an
        original, its edit and a redelivery of either stay distinguishable.
        """
        thread_id = (
            message.message_thread_id
            if getattr(message, "is_topic_message", False)
            else None
        )
        edit_date: Any = getattr(message, "edit_date", None)
        if hasattr(edit_date, "timestamp"):
            revision = int(edit_date.timestamp())
        else:
            revision = int(edit_date or 0)
        return TurnIdentity(
            chat_id=message.chat.id,
            message_id=message.message_id,
            kind=kind,
            revision=revision,
            thread_id=thread_id,
        )

    @staticmethod
    def _turn_artifacts(turn: dict[str, Any]) -> tuple[str, ...]:
        """Artifact paths the core asked us to deliver, bounded and typed.

        These are only *requests*; the presenter revalidates each one against
        its allow-list, so a hostile or buggy core cannot exfiltrate a file by
        naming it here.
        """
        raw = turn.get("artifacts")
        if not isinstance(raw, (list, tuple)):
            return ()
        return tuple(item for item in raw if isinstance(item, str) and item)[:10]

    async def _reply_with_voice_marker(self, message: Message, reply: str) -> None:
        """Send reply text; if it carries a ⟪voice:path⟫ marker, send the audio as a voice message."""
        import re as _re
        m = _re.search(r"⟪ voice:([^⟫]+)⟫", reply)
        if not m:
            await message.answer(reply, reply_to_message_id=message.message_id)
            return
        voice_path = m.group(1).strip()
        clean = (reply[: m.start()] + reply[m.end():]).strip()
        if clean:
            await message.answer(clean, reply_to_message_id=message.message_id)
        try:
            from pathlib import Path as _Path
            p = _Path(voice_path)
            if p.exists() and p.stat().st_size > 0:
                from aiogram.types import FSInputFile as _FSInputFile
                await message.answer_voice(
                    _FSInputFile(str(p)),
                    reply_to_message_id=message.message_id,
                )
                return
        except Exception:
            logger.exception("voice marker send failed for %s", voice_path)
        await message.answer("🎙 Голосовое не удалось отправить.", reply_to_message_id=message.message_id)

    async def _bridge_turn(
        self,
        message: Message,
        text: str,
        *,
        kind: str,
        attachment_ids: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        """Run exactly one agent turn for this update through the bridge."""
        self.telegram_bridge.rebind_gateway(self.gateway_client)
        result = await self.telegram_bridge.turn(
            identity=self._turn_identity(message, kind),
            text=text,
            user_id=getattr(message.from_user, "id", 0),
            attachment_ids=attachment_ids,
        )
        return result.payload

    # ── Inbound attachments — one secure abstraction for every media kind ─

    @staticmethod
    def _describe_attachment(message: Message, kind: str) -> _AttachmentRef | None:
        """Normalise a document/photo update into one transport descriptor."""
        if kind == "document":
            doc = message.document
            if doc is None:
                return None
            return _AttachmentRef(
                file_id=doc.file_id,
                filename=doc.file_name or "document",
                size=int(doc.file_size or 0),
                mime_type=doc.mime_type or "application/octet-stream",
            )
        if kind == "photo":
            photos = message.photo or []
            if not photos:
                return None
            largest = photos[-1]  # Telegram orders sizes ascending
            return _AttachmentRef(
                file_id=largest.file_id,
                filename=f"photo_{message.message_id}.jpg",
                size=int(largest.file_size or 0),
                mime_type="image/jpeg",
            )
        return None

    def _register_attachment(self, path: Path) -> str:
        """Bind a stored attachment to an opaque, bounded handle."""
        handle = secrets.token_urlsafe(12)
        self._attachments[handle] = path
        while len(self._attachments) > _MAX_TRACKED_ATTACHMENTS:
            self._attachments.popitem(last=False)
        return handle

    def resolve_attachment(self, handle: str) -> Path | None:
        """Resolve an opaque inbound-attachment handle, or ``None``."""
        return self._attachments.get(handle)

    # ── Owner gate (Level 1: Telegram ID) ────────────────────────────────
    # Fail-closed by construction: ``OwnerIdentity.is_owner`` already returns
    # False when ANTIGONA_OWNER_ID is unset, so an unconfigured deployment
    # denies everyone instead of promoting everyone to owner. The gate lives
    # in the handlers (not in a router middleware) on purpose: handlers are
    # also invoked directly — ``/list`` and ``/get`` delegate to
    # ``status_handler`` — and a middleware would not cover those call paths,
    # nor could it produce the per-command denial texts the auth commands need.
    @staticmethod
    def _is_owner_user(user_id: int) -> bool:
        """True only when ``user_id`` matches the configured owner."""
        from antigona.security.owner_identity import OwnerIdentity

        return OwnerIdentity().is_owner(user_id)

    async def _deny_if_not_owner(self, message: Message, *, what: str) -> bool:
        """Reject non-owners for a privileged command. True when denied."""
        user_id = getattr(message.from_user, "id", 0)
        if self._is_owner_user(user_id):
            return False
        logger.info("%s denied for user_id=%s (not owner)", what, user_id)
        await message.answer(
            "🚫 Доступ запрещён.",
            reply_to_message_id=message.message_id,
        )
        return True

    async def _deny_callback_if_not_owner(
        self, query: CallbackQuery, *, what: str
    ) -> bool:
        """Reject non-owners for a privileged inline action. True when denied."""
        user_id = getattr(query.from_user, "id", 0)
        if self._is_owner_user(user_id):
            return False
        logger.info("%s denied for user_id=%s (not owner)", what, user_id)
        await query.answer(text="🚫 Доступ запрещён.", show_alert=True)
        return True

    async def _handle_inbound_attachment(self, message: Message, *, kind: str) -> None:
        """Download safely, then hand off to the runtime exactly once."""
        bot_username, bot_id = await self._resolve_bot_info()
        if not should_process_message(message, bot_username, bot_id):
            return

        from antigona.security.owner_identity import OwnerIdentity

        user_id = getattr(message.from_user, "id", 0)
        owner = OwnerIdentity()
        if not owner.is_owner(user_id):
            logger.info("Attachment denied for user_id=%d (not owner)", user_id)
            await message.answer("🚫 Доступ запрещён.")
            return

        bot = message.bot
        ref = self._describe_attachment(message, kind)
        if bot is None or ref is None:
            return

        chat_id = message.chat.id
        slot = None
        # D-3: an attachment turn is a real operation and must leave the same
        # observability record (operations row + binding) as a text turn.
        _op_id: str | None = None
        _correlation = _generate_correlation_id()
        try:
            slot = prepare_inbound_file(
                paths.downloads_dir() / str(chat_id),
                ref.filename,
                size=ref.size,
                max_size=MAX_ATTACHMENT_BYTES,
            )
            file_info = await bot.get_file(ref.file_id)
            if not file_info.file_path:
                raise AttachmentRejected("Telegram returned no file path")
            # Write through the descriptor opened with O_EXCL|O_NOFOLLOW so the
            # bytes land in the object that was validated, not in a symlink
            # swapped in after the check.
            with os.fdopen(os.dup(slot.handle), "wb") as sink:
                await bot.download_file(file_info.file_path, destination=sink)
            stored_size = finalize_inbound_file(slot, max_size=MAX_ATTACHMENT_BYTES)

            handle = self._register_attachment(slot.path)
            caption = (message.caption or "").strip()
            handoff = (
                f"[attachment:{kind}] Пользователь прислал файл.\n"
                f"name: {slot.path.name}\n"
                f"original_name: {sanitize_filename(ref.filename)}\n"
                f"mime: {ref.mime_type}\n"
                f"size_bytes: {stored_size}\n"
                f"telegram_file_id: {ref.file_id}\n"
                f"handle: attachment://{handle}\n"
                f"path: {slot.path}\n"
                f"caption: {caption or '[нет]'}"
            )
            try:
                await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
            except Exception:
                pass
            try:
                operation = await self.operation_store.create(
                    chat_id=chat_id,
                    user_id=user_id,
                    text=handoff,
                    message_id=message.message_id,
                    reply_to_message_id=getattr(
                        message.reply_to_message, "message_id", None
                    ),
                    correlation_id=_correlation,
                )
                _op_id = operation.id
                await self.event_bus.publish(
                    OperationReceived(
                        correlation_id=_correlation,
                        operation_id=operation.id,
                        chat_id=chat_id,
                        user_id=user_id,
                        text=handoff,
                        message_id=message.message_id,
                    )
                )
                await self._publish_operation_stage(
                    _op_id, "CLASSIFYING", description="Обрабатываю вложение"
                )
            except Exception:
                logger.exception("Failed to create operation for attachment turn")
                _op_id = None

            turn = await self._bridge_turn(
                message, handoff, kind=kind, attachment_ids=(handle,)
            )
            reply = str(turn.get("reply") or "").strip() or "📎 Файл принят."
            attachment_outcome = str(turn.get("tool_outcome") or "").strip().upper()
            attachment_error = attachment_outcome in ("FAILED", "DENIED", "PARTIAL")
            if attachment_error:
                reply = _sanitize_user_facing_error(reply)
            # Deliver the reply exactly once over the transport, then record the
            # REAL returned message ids, an honest terminal state and the
            # inbound<->outbound correlation.  Delivery stays on message.answer
            # so the transport contract for attachments is unchanged.
            sent_messages: list[Any] = []
            if utf16_length(reply) <= TELEGRAM_TEXT_LIMIT:
                sent_messages.append(
                    await message.answer(
                        reply, reply_to_message_id=message.message_id
                    )
                )
            else:
                for chunk in split_telegram_html(reply):
                    sent_messages.append(
                        await message.answer(
                            chunk,
                            reply_to_message_id=message.message_id,
                            parse_mode=None,
                        )
                    )

            if _op_id is not None:
                # Real ids returned by the send calls — never a computed guess.
                real_ids: list[int] = []
                for sent in sent_messages:
                    candidate = getattr(sent, "message_id", None)
                    if (
                        isinstance(candidate, int)
                        and not isinstance(candidate, bool)
                        and candidate > 0
                    ):
                        real_ids.append(candidate)
                for mid in real_ids:
                    try:
                        await self.operation_store.add_final_message_id(_op_id, mid)
                    except Exception:
                        logger.exception(
                            "Failed to record attachment final message id"
                        )
                if attachment_error:
                    try:
                        await self.operation_store.set_last_error(
                            _op_id,
                            _safe_result_text(
                                turn.get("last_error") or reply,
                                fallback="attachment turn failed",
                                max_length=500,
                            ),
                        )
                    except Exception:
                        logger.exception("Failed to persist attachment last_error")
                await self._finalize_operation_state(
                    _op_id,
                    OperationState.FAILED
                    if attachment_error
                    else OperationState.SUCCEEDED,
                )
                for mid in real_ids:
                    try:
                        await self.binding_repo.save(
                            chat_id=chat_id,
                            telegram_message_id=mid,
                            user_id=None,
                            task_id=turn.get("flow_id"),
                            correlation_id=_correlation,
                            message_role="assistant",
                            message_kind="task_result",
                            source_message_id=message.message_id,
                        )
                    except Exception:
                        logger.exception(
                            "Failed to persist attachment result binding"
                        )

        except BridgeOverflow:
            await message.answer(
                "⏳ Очередь этого чата заполнена. Повторите позже.",
                reply_to_message_id=message.message_id,
            )
        except AmbiguousTurn:
            await message.answer(
                "⚠️ Этот файл уже обрабатывался до перезапуска; результат "
                "неизвестен. Отправьте его заново, если нужен повтор.",
                reply_to_message_id=message.message_id,
            )
        except AttachmentRejected:
            await self._fail_attachment_operation(
                _op_id, "Файл отклонён политикой безопасности."
            )
            await message.answer(
                "❌ Файл отклонён политикой безопасности.",
                reply_to_message_id=message.message_id,
            )
        except Exception:
            logger.exception("Secure attachment handoff failed for chat=%d", chat_id)
            await self._fail_attachment_operation(
                _op_id, "Не удалось обработать файл (внутренняя ошибка)."
            )
            await message.answer(
                "❌ Не удалось обработать файл. Подробности скрыты: [REDACTED]",
                reply_to_message_id=message.message_id,
            )
        finally:
            if slot is not None:
                discard_inbound_file(slot)
            self._update_last_response_time(chat_id)

    async def _finalize_operation_state(
        self, operation_id: str, target: OperationState
    ) -> bool:
        """Move an operation to a terminal state via the legal state machine.

        Mirrors the presenter commit path (SUCCEEDED goes through FINALIZING)
        so a directly-recorded operation is never left non-terminal.
        """
        try:
            data = await self.operation_store.get(operation_id)
        except Exception:
            logger.exception("Could not load operation for finalization")
            return False
        if data is None:
            return False
        try:
            current = OperationState(str(data.status).upper())
        except ValueError:
            return False
        if current is target:
            return True
        if StateMachine.is_terminal(current):
            return False
        if (
            target is OperationState.SUCCEEDED
            and current is not OperationState.FINALIZING
        ):
            if not await self.operation_store.transition_status(
                operation_id,
                OperationState.FINALIZING,
                expected_current=current,
            ):
                return False
            current = OperationState.FINALIZING
        return await self.operation_store.transition_status(
            operation_id,
            target,
            expected_current=current,
        )

    async def _fail_attachment_operation(
        self, operation_id: str | None, reason: str
    ) -> None:
        """Mark an attachment operation failed and record a real last_error."""
        if operation_id is None:
            return
        try:
            await self.operation_store.set_last_error(
                operation_id,
                _safe_result_text(reason, fallback="attachment failed", max_length=500),
            )
        except Exception:
            logger.exception("Failed to persist attachment operation error")
        await self._publish_operation_final(
            operation_id,
            f"❌ {reason}",
            terminal_state=OperationState.FAILED,
        )

    # ── Memory summarization helpers (Day 11) ───────────────────────────

    def _register_handlers(self) -> None:
        """Register message and callback handlers.

        Each handler is thin: receive → route → (plan) → respond.
        """

        @self.router.errors()
        async def global_error_handler(event: ErrorEvent) -> None:
            """Catch-all safety net for unhandled exceptions in any handler.

            Without this, aiogram just logs the exception and the user gets
            no reply at all — the bot appears to "hang" or ignore them. Every
            handler above already has local try/except for expected failure
            modes (Telegram API, LLM provider); this only fires for genuine
            bugs, so it always notifies the user instead of staying silent.
            """
            logger.exception(
                "Unhandled exception processing update %s: %s",
                getattr(event.update, "update_id", "?"),
                event.exception,
            )
            source_message = (
                event.update.message
                or event.update.edited_message
                or (event.update.callback_query.message if event.update.callback_query else None)
            )
            if source_message is not None:
                try:
                    await source_message.answer(
                        "⚠️ Произошла внутренняя ошибка при обработке сообщения. "
                        "Попробуйте ещё раз или чуть позже."
                    )
                except Exception:
                    logger.warning(
                        "Failed to deliver error-fallback notice to chat=%s",
                        getattr(source_message.chat, "id", "?"),
                        exc_info=True,
                    )

        @self.router.message(Command("start"))
        async def start_handler(message: Message) -> None:
            chat_id = message.chat.id
            cid = _generate_correlation_id()

            # Publish event
            await self.event_bus.publish(
                MessageReceived(
                    correlation_id=cid,
                    chat_id=chat_id,
                    user_id=getattr(message.from_user, "id", 0),
                    text=message.text or "",
                    message_id=message.message_id,
                )
            )

            await self._enforce_rate_limit(chat_id)
            reply_text = (
                "🤖 <b>Antigona — твой AI-агент в Telegram</b>\n\n"
                "Отправь задачу, и я создам flow и выполню её.\n"
                "Пример: <code>create file hello.txt with content Hello World</code>\n\n"
                "🎨 <code>/image &lt;описание&gt;</code> — сгенерировать картинку (бесплатно)\n"
                "🎙 <code>/tts &lt;текст&gt;</code> — отправить голосовое сообщение\n"
                "🔧 <code>/install &lt;pip|npm|apt&gt; &lt;пакет&gt;</code> — установить инструмент\n"
                "🌐 <code>/web &lt;запрос&gt;</code> — поиск в интернете\n"
                "🩺 <code>/health</code> — здоровье сервисов\n"
                "🧠 <code>/memory &lt;текст&gt;</code> — память агента\n"
                "📋 <code>/help</code> — все команды"
            )
            await message.answer(
                reply_text,
                reply_to_message_id=message.message_id,
            )
            self._update_last_response_time(chat_id)

            await self.event_bus.publish(
                ConversationReply(
                    correlation_id=cid,
                    chat_id=chat_id,
                    text=reply_text,
                    intent="command.help",
                )
            )

        @self.router.message(Command("help", "commands"))
        async def help_handler(message: Message) -> None:
            chat_id = message.chat.id
            cid = _generate_correlation_id()

            await self.event_bus.publish(
                MessageReceived(
                    correlation_id=cid,
                    chat_id=chat_id,
                    user_id=getattr(message.from_user, "id", 0),
                    text=message.text or "",
                    message_id=message.message_id,
                )
            )

            await self._enforce_rate_limit(chat_id)

            reply_text = _build_help_text()
            await message.answer(
                reply_text,
                reply_to_message_id=message.message_id,
                parse_mode="HTML",
            )
            self._update_last_response_time(chat_id)

            await self.event_bus.publish(
                ConversationReply(
                    correlation_id=cid,
                    chat_id=chat_id,
                    text=reply_text,
                    intent="command.help",
                )
            )


        @self.router.message(Command("health"))
        async def health_handler(message: Message) -> None:
            """Структурированное здоровье: gateway / worker / verifier / время."""
            chat_id = message.chat.id
            cid = _generate_correlation_id()
            await self.event_bus.publish(
                MessageReceived(
                    correlation_id=cid, chat_id=chat_id,
                    user_id=getattr(message.from_user, "id", 0),
                    text=message.text or "", message_id=message.message_id,
                )
            )
            await self._enforce_rate_limit(chat_id)
            try:
                data = await self.gateway_client.health()
                ok = bool(data and (data.get("status") in ("ok", "healthy") or data.get("ok")))
                gateway = "ok" if ok else "отвечает, но статус не OK"
            except Exception as exc:
                logger.warning("Gateway health failed: %s", exc)
                gateway = "down"
            reply = collect_health_report(gateway)
            await message.answer(reply, reply_to_message_id=message.message_id)
            self._update_last_response_time(chat_id)

        @self.router.message(Command("tasks"))
        async def tasks_handler(message: Message) -> None:
            """Алиас /list — список задач."""
            await status_handler(message)

        @self.router.message(Command("session"))
        async def session_handler(message: Message) -> None:
            """Информация о текущей сессии через Gateway."""
            chat_id = message.chat.id
            cid = _generate_correlation_id()
            user_id = getattr(message.from_user, "id", 0)
            await self.event_bus.publish(
                MessageReceived(
                    correlation_id=cid, chat_id=chat_id,
                    user_id=user_id, text=message.text or "",
                    message_id=message.message_id,
                )
            )
            await self._enforce_rate_limit(chat_id)
            session_id = f"telegram:{user_id}"
            try:
                data = await self.gateway_client.session_info(session_id)
                lines_ = ["📋 Сессия:"]
                if isinstance(data, dict):
                    for k in ("session_id", "flow_count", "message_count", "created_at"):
                        if k in data:
                            lines_.append(f"  • {k}: {data[k]}")
                reply = "\n".join(lines_)
            except Exception as exc:
                logger.warning("Gateway session_info failed: %s", exc)
                reply = "❌ Не удалось получить информацию о сессии через Gateway."
            await message.answer(reply, reply_to_message_id=message.message_id)
            self._update_last_response_time(chat_id)

        @self.router.message(Command("history"))
        async def history_handler(message: Message) -> None:
            """История переписки сессии через Gateway."""
            chat_id = message.chat.id
            cid = _generate_correlation_id()
            user_id = getattr(message.from_user, "id", 0)
            await self.event_bus.publish(
                MessageReceived(
                    correlation_id=cid, chat_id=chat_id,
                    user_id=user_id, text=message.text or "",
                    message_id=message.message_id,
                )
            )
            await self._enforce_rate_limit(chat_id)
            session_id = f"telegram:{user_id}"
            try:
                data = await self.gateway_client.session_history(session_id, limit=20)
                rows = data.get("messages", []) if isinstance(data, dict) else []
                if not rows:
                    reply = "📋 История сессии пуста."
                else:
                    lines_ = ["📜 История (последние сообщения):"]
                    for m in rows[-20:]:
                        role = str(m.get("role", "?"))
                        text = str(m.get("text", m.get("content", "")))[:80]
                        lines_.append(f"  • <b>{role}</b>: {text}")
                    reply = "\n".join(lines_)
            except Exception as exc:
                logger.warning("Gateway session_history failed: %s", exc)
                reply = "❌ Не удалось получить историю через Gateway."
            await message.answer(reply, parse_mode="HTML", reply_to_message_id=message.message_id)
            self._update_last_response_time(chat_id)

        @self.router.message(Command("memory"))
        async def memory_handler(message: Message) -> None:
            """Память агента через Gateway."""
            chat_id = message.chat.id
            cid = _generate_correlation_id()
            await self.event_bus.publish(
                MessageReceived(
                    correlation_id=cid, chat_id=chat_id,
                    user_id=getattr(message.from_user, "id", 0),
                    text=message.text or "", message_id=message.message_id,
                )
            )
            await self._enforce_rate_limit(chat_id)
            try:
                data = await self.gateway_client.memory_list(limit=20)
                entries = data.get("entries", data.get("items", [])) if isinstance(data, dict) else []
                if not entries:
                    reply = "🧠 Память пуста."
                else:
                    lines_ = ["🧠 Память агента:"]
                    for e in entries[:20]:
                        content = str(e.get("content", ""))[:80]
                        kind = str(e.get("kind", ""))
                        lines_.append(f"  • [{kind}] {content}")
                    reply = "\n".join(lines_)
            except Exception as exc:
                logger.warning("Gateway memory failed: %s", exc)
                reply = "❌ Не удалось получить память через Gateway."
            await message.answer(reply, reply_to_message_id=message.message_id)
            self._update_last_response_time(chat_id)

        @self.router.message(Command("status"))
        async def status_handler(message: Message) -> None:
            """Статус задач — только через Gateway (Step 4/12)."""
            chat_id = message.chat.id
            cid = _generate_correlation_id()
            await self.event_bus.publish(
                MessageReceived(
                    correlation_id=cid, chat_id=chat_id,
                    user_id=getattr(message.from_user, "id", 0),
                    text=message.text or "", message_id=message.message_id,
                )
            )
            await self._enforce_rate_limit(chat_id)
            parts = (message.text or "").strip().split(maxsplit=1)
            flow_id = parts[1].strip() if len(parts) > 1 else ""
            try:
                if flow_id:
                    flow = await self.gateway_client.get_flow(flow_id)
                    status = str(getattr(flow, "status", "unknown"))
                    reply = f"📋 Задача <code>{flow_id[:12]}</code>: <b>{status}</b>"
                else:
                    flows = await self.gateway_client.list_flows(limit=10)
                    items = getattr(flows, "items", None)
                    if items is None and isinstance(flows, dict):
                        items = flows.get("items", [])
                    rows = list(items or [])
                    if not rows:
                        reply = "📋 Нет задач."
                    else:
                        lines_ = ["📋 Последние задачи:"]
                        for f in rows:
                            fid = str(getattr(f, "id", "") or "")[:12]
                            st = str(getattr(f, "status", "") or "").upper()
                            goal = str(getattr(f, "goal", "") or "")[:60]
                            lines_.append(f"  • <code>{fid}</code> [{st}] {goal}")
                        reply = "\n".join(lines_)
            except GatewayFlowNotFoundError:
                reply = f"📋 Задача <code>{flow_id[:12]}</code> не найдена."
            except Exception as exc:
                logger.warning("Gateway status failed: %s", exc)
                reply = "❌ Не удалось получить статус через Gateway."
            await message.answer(reply, parse_mode="HTML",
                                 reply_to_message_id=message.message_id)
            self._update_last_response_time(chat_id)

        @self.router.message(Command("list"))
        async def list_handler(message: Message) -> None:
            """Список задач — только через Gateway."""
            await status_handler(message)

        @self.router.message(Command("get"))
        async def get_handler(message: Message) -> None:
            """Состояние задачи — только через Gateway."""
            await status_handler(message)

        @self.router.message(Command("cancel"))
        async def cancel_handler(message: Message) -> None:
            """Отмена задачи — только через Gateway."""
            if await self._deny_if_not_owner(message, what="/cancel"):
                return
            chat_id = message.chat.id
            cid = _generate_correlation_id()
            await self.event_bus.publish(
                MessageReceived(
                    correlation_id=cid, chat_id=chat_id,
                    user_id=getattr(message.from_user, "id", 0),
                    text=message.text or "", message_id=message.message_id,
                )
            )
            await self._enforce_rate_limit(chat_id)
            parts = (message.text or "").strip().split(maxsplit=1)
            flow_id = parts[1].strip() if len(parts) > 1 else ""
            if not flow_id:
                await message.answer(
                    "🚫 Использование: /cancel <flow_id>",
                    reply_to_message_id=message.message_id,
                )
                return
            try:
                await self.gateway_client.cancel(flow_id=flow_id, reason="user /cancel")
                reply = f"🚫 Запрос на отмену {flow_id[:12]} отправлен в Gateway."
            except Exception as exc:
                logger.warning("Gateway cancel failed: %s", exc)
                reply = "❌ Не удалось отменить задачу через Gateway."
            await message.answer(reply, reply_to_message_id=message.message_id)
            self._update_last_response_time(chat_id)

        @self.router.message(Command("stop"))
        async def stop_handler(message: Message) -> None:
            """Cancel *this chat's* work: queued turns here, active flow in core.

            Session-scoped on purpose — the user should not have to quote a
            flow id (that is ``/cancel``), and one chat must never be able to
            stop another chat's work.
            """
            from antigona.security.owner_identity import OwnerIdentity

            chat_id = message.chat.id
            user_id = getattr(message.from_user, "id", 0)
            owner = OwnerIdentity()
            if not owner.is_owner(user_id):
                logger.info("/stop denied for user_id=%d (not owner)", user_id)
                await message.answer("🚫 Доступ запрещён.")
                return

            cid = _generate_correlation_id()
            await self.event_bus.publish(
                MessageReceived(
                    correlation_id=cid, chat_id=chat_id, user_id=user_id,
                    text=message.text or "", message_id=message.message_id,
                )
            )
            await self._enforce_rate_limit(chat_id)

            dropped = await self.telegram_bridge.cancel_chat(chat_id)

            active = None
            try:
                active = await self.operation_store.find_active_by_chat(chat_id)
            except Exception:
                logger.exception("Failed to look up active operation for /stop")

            flow_id = getattr(active, "flow_id", None) if active is not None else None
            parts: list[str] = []
            if dropped:
                parts.append(f"🚫 Снято из очереди: {dropped}.")
            if flow_id:
                try:
                    await self.gateway_client.cancel(
                        flow_id=flow_id,
                        reason="user /stop",
                    )
                    parts.append(
                        f"🚫 Запрос на отмену {flow_id[:12]} отправлен в ядро."
                    )
                except Exception as exc:
                    logger.warning("Gateway cancel failed for /stop: %s", exc)
                    parts.append("❌ Ядро не приняло запрос на отмену.")
            elif active is not None:
                parts.append("⏳ Активная операция ещё не создала задачу в ядре.")
            if not parts:
                parts.append("ℹ️ Нет активной работы в этом чате.")

            await message.answer(
                "\n".join(parts),
                reply_to_message_id=message.message_id,
            )
            self._update_last_response_time(chat_id)

        @self.router.message(Command("reset", "exit", "quit"))
        async def reset_handler(message: Message) -> None:
            """End this chat's conversation. Never touches the bot process.

            Task 1 fix: /exit previously had no Telegram handler at all (it
            was CLI-only in the command registry, where it means
            process.exit()), so it fell through to unknown-command handling
            and follow-up text kept getting answered as free-form dialogue
            against stale context. A shared bot process must not die because
            one chat typed /exit — instead this clears that chat's history
            and any tracked active flow, same idea, chat-scoped.
            """
            chat_id = message.chat.id
            user_id = getattr(message.from_user, "id", 0)
            cid = _generate_correlation_id()
            await self.event_bus.publish(
                MessageReceived(
                    correlation_id=cid, chat_id=chat_id, user_id=user_id,
                    text=message.text or "", message_id=message.message_id,
                )
            )
            await self._enforce_rate_limit(chat_id)
            session_id = f"telegram:{user_id}"
            try:
                await self.gateway_client.reset_session(session_id)
                reply = "🔄 Диалог завершён. Начнём с чистого листа."
            except Exception as exc:
                logger.warning("Gateway session reset failed: %s", exc)
                reply = "❌ Не удалось сбросить диалог через Gateway."
            await message.answer(reply, reply_to_message_id=message.message_id)
            self._update_last_response_time(chat_id)

        @self.router.message(Command("steer"))
        async def steer_handler(message: Message) -> None:
            """Скорректировать задачу — только через Gateway."""
            if await self._deny_if_not_owner(message, what="/steer"):
                return
            chat_id = message.chat.id
            cid = _generate_correlation_id()
            await self.event_bus.publish(
                MessageReceived(
                    correlation_id=cid, chat_id=chat_id,
                    user_id=getattr(message.from_user, "id", 0),
                    text=message.text or "", message_id=message.message_id,
                )
            )
            await self._enforce_rate_limit(chat_id)
            parts = (message.text or "").strip().split(maxsplit=2)
            if len(parts) < 3:
                await message.answer(
                    "🎯 Использование: /steer <flow_id> <текст корректировки>",
                    reply_to_message_id=message.message_id,
                )
                return
            flow_id, steer_text = parts[1].strip(), parts[2].strip()
            from antigona.core.control_plane import SteeringCommand
            try:
                await self.gateway_client.steer(
                    flow_id,
                    SteeringCommand(
                        flow_id=flow_id,
                        command="modify",
                        modification_text=steer_text,
                        correlation_id=cid,
                    ),
                )
                reply = f"🎯 Задача {flow_id[:12]} скорректирована."
            except Exception as exc:
                logger.warning("Gateway steer failed: %s", exc)
                reply = "❌ Не удалось скорректировать задачу через Gateway."
            await message.answer(reply, reply_to_message_id=message.message_id)
            self._update_last_response_time(chat_id)

        @self.router.message(Command("approvals"))
        async def approvals_handler(message: Message) -> None:
            """Список одобрений — только через Gateway."""
            if await self._deny_if_not_owner(message, what="/approvals"):
                return
            chat_id = message.chat.id
            cid = _generate_correlation_id()
            await self.event_bus.publish(
                MessageReceived(
                    correlation_id=cid, chat_id=chat_id,
                    user_id=getattr(message.from_user, "id", 0),
                    text=message.text or "", message_id=message.message_id,
                )
            )
            await self._enforce_rate_limit(chat_id)
            try:
                approvals_list = await self.gateway_client.list_approvals(
                    status="PENDING", limit=50
                )
                items = getattr(approvals_list, "items", None)
                if items is None and isinstance(approvals_list, dict):
                    items = approvals_list.get("items", [])
                rows = list(items or [])
                if not rows:
                    reply = "📋 Нет ожидающих одобрений."
                else:
                    lines_ = ["📋 Ожидающие одобрения:"]
                    for a in rows:
                        aid = str(getattr(a, "id", "") or "")[:12]
                        risk = str(getattr(a, "risk_level", "") or "")
                        reason = str(getattr(a, "reason", "") or "")[:60]
                        lines_.append(f"  • <code>{aid}</code> [{risk}] {reason}")
                    reply = "\n".join(lines_)
            except Exception as exc:
                logger.warning("Gateway approvals failed: %s", exc)
                reply = "❌ Не удалось получить одобрения через Gateway."
            await message.answer(reply, parse_mode="HTML",
                                 reply_to_message_id=message.message_id)
            self._update_last_response_time(chat_id)

        @self.router.message(Command("approve"))
        async def approve_handler(message: Message) -> None:
            """Одобрить операцию — только через Gateway."""
            if await self._deny_if_not_owner(message, what="/approve"):
                return
            chat_id = message.chat.id
            cid = _generate_correlation_id()
            await self.event_bus.publish(
                MessageReceived(
                    correlation_id=cid, chat_id=chat_id,
                    user_id=getattr(message.from_user, "id", 0),
                    text=message.text or "", message_id=message.message_id,
                )
            )
            await self._enforce_rate_limit(chat_id)
            parts = (message.text or "").strip().split(maxsplit=1)
            approval_id = parts[1].strip() if len(parts) > 1 else ""
            if not approval_id:
                # Документированное поведение: без ID — авто-выбор единственного pending.
                def _aid(a: Any) -> str:
                    if isinstance(a, dict):
                        return str(a.get("id", "") or "")
                    return str(getattr(a, "id", "") or "")

                try:
                    approvals_list = await self.gateway_client.list_approvals(
                        status="PENDING", limit=50
                    )
                    items = getattr(approvals_list, "items", None)
                    if items is None and isinstance(approvals_list, dict):
                        items = approvals_list.get("items", [])
                    rows = list(items or [])
                except Exception as exc:
                    logger.warning("Gateway approvals list failed: %s", exc)
                    rows = []
                if len(rows) == 1:
                    approval_id = _aid(rows[0])
                elif len(rows) > 1:
                    lines_ = ["🔐 Несколько ожидающих одобрений — укажи ID:", ""]
                    for a in rows:
                        risk = a.get("risk_level", "") if isinstance(a, dict) else getattr(a, "risk_level", "")
                        reason = (a.get("reason", "") if isinstance(a, dict) else getattr(a, "reason", ""))[:60]
                        lines_.append(
                            f"  • <code>{html.escape(_aid(a)[:12])}</code> "
                            f"[{html.escape(str(risk))}] {html.escape(str(reason))}"
                        )
                    lines_.extend(["", "Использование: /approve &lt;approval_id&gt;"])
                    await message.answer(
                        "\n".join(lines_), parse_mode="HTML",
                        reply_to_message_id=message.message_id,
                    )
                    self._update_last_response_time(chat_id)
                    return
                elif len(rows) == 0:
                    await message.answer(
                        "📋 Нет ожидающих одобрений.",
                        reply_to_message_id=message.message_id,
                    )
                    self._update_last_response_time(chat_id)
                    return
            if not approval_id:
                await message.answer(
                    "✅ Использование: /approve <approval_id>",
                    reply_to_message_id=message.message_id,
                )
                return
            try:
                await self.gateway_client.decide_approval(approval_id, approve=True)
                reply = f"✅ Одобрение {approval_id[:12]} принято."
            except Exception as exc:
                logger.warning("Gateway approve failed: %s", exc)
                reply = "❌ Не удалось одобрить через Gateway."
            await message.answer(reply, reply_to_message_id=message.message_id)
            self._update_last_response_time(chat_id)

        @self.router.message(Command("deny"))
        async def deny_handler(message: Message) -> None:
            """Отклонить операцию — только через Gateway."""
            if await self._deny_if_not_owner(message, what="/deny"):
                return
            chat_id = message.chat.id
            cid = _generate_correlation_id()
            await self.event_bus.publish(
                MessageReceived(
                    correlation_id=cid, chat_id=chat_id,
                    user_id=getattr(message.from_user, "id", 0),
                    text=message.text or "", message_id=message.message_id,
                )
            )
            await self._enforce_rate_limit(chat_id)
            parts = (message.text or "").strip().split(maxsplit=1)
            approval_id = parts[1].strip() if len(parts) > 1 else ""
            if not approval_id:
                await message.answer(
                    "🚫 Использование: /deny <approval_id>",
                    reply_to_message_id=message.message_id,
                )
                return
            try:
                await self.gateway_client.decide_approval(approval_id, approve=False)
                reply = f"🚫 Одобрение {approval_id[:12]} отклонено."
            except Exception as exc:
                logger.warning("Gateway deny failed: %s", exc)
                reply = "❌ Не удалось отклонить через Gateway."
            await message.answer(reply, reply_to_message_id=message.message_id)
            self._update_last_response_time(chat_id)

        @self.router.message(F.voice | F.audio)
        async def voice_message_handler(message: Message) -> None:
            """Голосовое сообщение — транспортный ввод → ядро (Turn API).

            STT — транспортное преобразование голоса в текст; распознанный
            текст уходит в единый Gateway Turn API, как обычное сообщение.
            """
            bot_username, bot_id = await self._resolve_bot_info()
            if not should_process_message(message, bot_username, bot_id):
                return

            # Owner gate (Level 1: Telegram ID) — same fail-closed contract as
            # text_handler / attachments / commands / callbacks. R1-TELEGRAM-01:
            # voice was owner-blind, so a non-owner could drive the core through
            # STT → Turn API.
            if await self._deny_if_not_owner(message, what="voice message"):
                return

            chat_id = message.chat.id
            cid = _generate_correlation_id()
            user_id = getattr(message.from_user, "id", 0)

            await self.event_bus.publish(
                MessageReceived(
                    correlation_id=cid, chat_id=chat_id,
                    user_id=user_id,
                    text="[voice message]", message_id=message.message_id,
                )
            )

            bot = message.bot
            if bot is None:
                return
            try:
                await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
            except Exception:
                pass

            try:
                voice = message.voice or message.audio
                assert voice is not None
                file_info = await bot.get_file(voice.file_id)
                file_path = file_info.file_path
                if not file_path:
                    await message.answer("❌ Не удалось получить файл.")
                    return
                import os
                dest_dir = str(paths.voice_cache_dir())
                os.makedirs(dest_dir, exist_ok=True)
                dest_path = os.path.join(dest_dir, f"voice_{cid}.ogg")
                await bot.download_file(file_path, destination=dest_path)

                text = await speech_to_text(dest_path)
                if not text:
                    await message.answer(
                        "❌ Не удалось распознать голосовое сообщение.",
                    )
                    return

                await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
                try:
                    turn = await self._bridge_turn(message, text, kind="voice")
                except Exception as exc:
                    logger.warning("Gateway dialogue turn (voice) failed: %s", exc)
                    await message.answer(
                        "❌ Не удалось обработать голосовое сообщение. "
                        "Подробности скрыты: [REDACTED]",
                        reply_to_message_id=message.message_id,
                    )
                    return
                reply = str(turn.get("reply") or "").strip() or "⚠️ Обработка завершилась без ответа (пустой результат)."
                sent = await message.answer(
                    reply,
                    reply_to_message_id=message.message_id,
                )
                try:
                    await self.binding_repo.save(
                        chat_id=chat_id,
                        telegram_message_id=sent.message_id,
                        user_id=None,
                        source_message_id=message.message_id,
                        task_id=turn.get("flow_id"),
                        correlation_id=cid,
                        message_role="assistant",
                        message_kind="task_result",
                    )
                except Exception:
                    pass
            except Exception:
                await message.answer(
                    "❌ Не удалось обработать голосовое сообщение. "
                    "Подробности скрыты: [REDACTED]",
                    reply_to_message_id=message.message_id,
                )
            finally:
                self._update_last_response_time(chat_id)

        # ── Edited message handler ─────────────────────────────────────────

        @self.router.edited_message()
        async def edited_message_handler(message: Message) -> None:
            """Отредактированное сообщение — правка уходит в ядро (Turn API).

            Редактирование переотправляет новый текст в Gateway; ядро решает,
            скорректировать ли активную задачу (steering) или ответить.
            """
            bot_username, bot_id = await self._resolve_bot_info()
            if not should_process_message(message, bot_username, bot_id):
                return

            # Owner gate (Level 1: Telegram ID) — same fail-closed contract as
            # text_handler / attachments / commands / callbacks. R1-TELEGRAM-01:
            # message-edit was owner-blind, so a non-owner could steer/drive the
            # core by editing a message.
            if await self._deny_if_not_owner(message, what="edited message"):
                return

            new_text = (message.text or "").strip()
            if not new_text:
                return

            chat_id = message.chat.id
            msg_id = message.message_id
            user_id = getattr(message.from_user, "id", 0)
            cid = _generate_correlation_id()

            await self.event_bus.publish(
                MessageReceived(
                    correlation_id=cid,
                    chat_id=chat_id,
                    user_id=user_id,
                    text=new_text,
                    message_id=msg_id,
                )
            )

            try:
                turn = await self._bridge_turn(
                    message,
                    f"[правка сообщения] {new_text}",
                    kind="edited",
                )
            except Exception as exc:
                logger.warning("Gateway dialogue turn (edit) failed: %s", exc)
                await message.answer(
                    "✏️ Не удалось обработать правку. "
                    "Подробности скрыты: [REDACTED]",
                    reply_to_message_id=msg_id,
                )
                self._update_last_response_time(chat_id)
                return

            reply = str(turn.get("reply") or "").strip()
            if reply:
                sent = await message.answer(
                    f"✏️ {reply}",
                    reply_to_message_id=msg_id,
                )
                try:
                    await self.binding_repo.save(
                        chat_id=chat_id,
                        telegram_message_id=sent.message_id,
                        user_id=None,
                        source_message_id=message.message_id,
                        task_id=turn.get("flow_id"),
                        correlation_id=cid,
                        message_role="assistant",
                        message_kind="task_result",
                    )
                except Exception:
                    pass
            else:
                await message.answer(
                    f"✏️ Исправление принято (v: {msg_id}).",
                    reply_to_message_id=msg_id,
                )
            self._update_last_response_time(chat_id)

        @self.router.message(F.document)
        async def document_handler(message: Message) -> None:
            """Secure document download followed by one canonical runtime turn."""
            await self._handle_inbound_attachment(message, kind="document")

        @self.router.message(F.photo)
        async def photo_handler(message: Message) -> None:
            """Photos go through the same secure attachment abstraction."""
            await self._handle_inbound_attachment(message, kind="photo")

        @self.router.callback_query(ApprovalCallback.filter())
        async def approval_callback_handler(
            query: CallbackQuery, callback_data: ApprovalCallback
        ) -> None:
            if await self._deny_callback_if_not_owner(query, what="approval button"):
                return
            action = callback_data.action
            approve = action == "approve"
            cid = _generate_correlation_id()

            # A live button packs only a compact token; resolve it back to the
            # real (approval_id, flow_id). Legacy/direct callbacks may carry the
            # id inline (backward compatibility). Unknown/expired tokens are
            # answered with a clear notice instead of crashing.
            approval_id = callback_data.approval_id
            flow_id = callback_data.flow_id
            entry = resolve_approval_token(approval_id)
            if entry is not None:
                approval_id, flow_id = entry
            elif is_approval_token(approval_id):
                await query.answer(
                    "⏰ Кнопка устарела или недействительна. Запросите новую карточку.",
                    show_alert=True,
                )
                return

            # Publish event for approval action
            msg_chat = getattr(query.message, "chat", None)
            msg_chat_id = getattr(msg_chat, "id", 0) if msg_chat else 0
            await self.event_bus.publish(
                MessageReceived(
                    correlation_id=cid,
                    chat_id=msg_chat_id,
                    user_id=getattr(query.from_user, "id", 0),
                    text=f"approval:{approval_id}:{action}",
                    message_id=0,
                )
            )

            # If rejection, publish CancelRequested
            if action == "reject" and flow_id:
                await self.event_bus.request_cancel(
                    task_id=flow_id,
                    reason=f"approval rejection: {approval_id}",
                    correlation_id=cid,
                )

            # Idempotency: skip if already processed
            if self._is_approval_processed(approval_id, action):
                await query.answer(
                    text=f"✅ Уже {action == 'approve' and 'approved' or 'rejected'}",
                    show_alert=False,
                )
                return

            self._mark_approval_processed(approval_id, action)

            try:
                res = await self.post_approval_decision(approval_id, approve=approve)
                decision = res.get("decision", action)
                await query.answer(
                    text=f"✅ Decision submitted: {decision}", show_alert=False
                )
            except Exception:
                # Roll back idempotency key on failure so user can retry
                self._processed_approvals.discard(self._approval_key(approval_id, action))
                await query.answer(
                    text="❌ Не удалось отправить решение. Подробности скрыты: [REDACTED]",
                    show_alert=True,
                )
                return

            # Approval state reducer: update the card
            await self._update_approval_card(query, approval_id, action, decision)

        @self.router.message(Command("pin"))
        async def pin_handler(message: Message) -> None:
            """Verify PIN for privileged actions in Telegram.

            The configured owner is the sole admin. Dangerous actions
            (WRITE_FILE, RUN_SHELL, RUN_CODE, CONFIGURE_KEY, SEND_FILE,
            SEARCH_FILES) are blocked until the correct PIN is entered here.
            Verified state lasts 5 minutes.
            """
            from antigona.tools.pin_gate import (
                attempt_unlock,
                check_unlock_possible,
                elevate_session,
                is_pin_configured,
                mark_verified,
            )

            chat_id = message.chat.id
            text = message.text or ""
            parts = text.strip().split(maxsplit=1)
            attempt = parts[1].strip() if len(parts) > 1 else ""

            # P1 (Phase 7 Step 7.1): Owner ID is checked BEFORE the PIN.
            # /pin must not grant elevation to non-owners (security review F1/F3).
            from antigona.security.owner_identity import OwnerIdentity
            user_id = getattr(message.from_user, "id", 0)
            identity = OwnerIdentity()
            if not identity.is_owner(user_id):
                await message.answer(
                    "🚫 Доступ запрещён. Этот бот предназначен только для владельца."
                )
                return

            if not is_pin_configured():
                await message.answer(
                    "🔓 PIN не настроен. Опасные действия не заблокированы.\n"
                    "Задайте ANTIGONA_PIN в окружении сервера (через CLI)."
                )
                return

            if not attempt:
                allowed, msg = check_unlock_possible(chat_id)
                if not allowed:
                    await message.answer(f"🔒 {msg}")
                    return
                await message.answer("🔒 Использование: /pin <код>")
                return

            # Same entry point as /unlock: attempt_unlock applies the attempt
            # counter and the lockout window. Calling verify_pin directly here
            # used to offer unlimited, unthrottled guesses.
            success, msg = attempt_unlock(chat_id, attempt)

            # The PIN is plaintext in the chat log until this succeeds.
            try:
                await message.delete()
            except Exception:
                logger.debug(
                    "Failed to delete /pin message in chat_id=%s", chat_id,
                    exc_info=True,
                )

            if success:
                mark_verified(chat_id)
                # Clears the attempt counter, same as the /unlock path.
                elevate_session(chat_id, user_id)
                await message.answer(
                    "✅ PIN принят. Привилегированные действия разблокированы."
                )
            else:
                await message.answer(f"❌ {msg}")

        @self.router.message(Command("unlock"))
        async def unlock_handler(message: Message) -> None:
            from antigona.channels.telegram.auth_handlers import cmd_unlock
            await cmd_unlock(message)

        @self.router.message(Command("lock"))
        async def lock_handler(message: Message) -> None:
            from antigona.channels.telegram.auth_handlers import cmd_lock
            await cmd_lock(message)

        @self.router.message(Command("auth_status"))
        async def auth_status_handler(message: Message) -> None:
            from antigona.channels.telegram.auth_handlers import cmd_auth_status
            await cmd_auth_status(message)

        @self.router.message(Command("confirm"))
        async def confirm_handler(message: Message) -> None:
            from antigona.channels.telegram.auth_handlers import cmd_confirm
            await cmd_confirm(message)

        # ── Brain-routed management commands (thin transport to the core) ──
        # /install, /mcp, /plugins, /cli are answered deterministically by the
        # server brain via the Turn API (command.install / command.mcp /
        # command.plugins / command.cli). The bot is a thin transport: it only
        # forwards the raw slash command and displays the brain's reply.
        async def _brain_command_reply(message: Message, kind: str) -> None:
            # Owner gate (Level 1: Telegram ID) — same contract as /pin, /stop,
            # and the main text handler. These commands mutate runtime state
            # (MCP registry, plugins), so they must never be owner-blind.
            from antigona.security.owner_identity import OwnerIdentity
            chat_id = message.chat.id
            user_id = getattr(message.from_user, "id", 0)
            owner = OwnerIdentity()
            if not owner.is_owner(user_id):
                logger.info("/%s denied for user_id=%d (not owner)", kind, user_id)
                await message.answer("🚫 Доступ запрещён.")
                return
            cid = _generate_correlation_id()
            await self.event_bus.publish(
                MessageReceived(
                    correlation_id=cid, chat_id=chat_id,
                    user_id=user_id,
                    text=message.text or "", message_id=message.message_id,
                )
            )
            await self._enforce_rate_limit(chat_id)
            try:
                turn = await self._bridge_turn(message, message.text or "", kind=kind)
                reply = str(turn.get("reply") or "").strip() or "Готово."
            except Exception as exc:
                logger.warning("Telegram %s failed: %s", kind, exc)
                reply = "❌ Не удалось выполнить команду."
            await message.answer(reply, reply_to_message_id=message.message_id)
            self._update_last_response_time(chat_id)

        @self.router.message(Command("install"))
        async def install_handler(message: Message) -> None:
            await _brain_command_reply(message, "install")

        @self.router.message(Command("mcp"))
        async def mcp_handler(message: Message) -> None:
            await _brain_command_reply(message, "mcp")

        @self.router.message(Command("plugins"))
        async def plugins_handler(message: Message) -> None:
            await _brain_command_reply(message, "plugins")

        @self.router.message(Command("cli"))
        async def cli_handler(message: Message) -> None:
            await _brain_command_reply(message, "cli")

        @self.router.message(F.text.startswith("/"))
        async def unknown_command_handler(message: Message) -> None:
            """Неизвестная команда — чёткий ответ вместо LLM-free-text."""
            chat_id = message.chat.id
            cid = _generate_correlation_id()
            await self.event_bus.publish(
                MessageReceived(
                    correlation_id=cid, chat_id=chat_id,
                    user_id=getattr(message.from_user, "id", 0),
                    text=message.text or "", message_id=message.message_id,
                )
            )
            await self._enforce_rate_limit(chat_id)
            reply = unknown_command_reply(message.text or "")
            await message.answer(reply, reply_to_message_id=message.message_id)
            self._update_last_response_time(chat_id)

        @self.router.message(F.text)
        async def text_handler(message: Message) -> None:
            """Главный текстовый хендлер — тонкий транспорт до ядра.

            Step 4 (манифест): бот НЕ классифицирует интенты, не маршрутизирует,
            не создаёт задачи локально и не исполняет действия. Весь текст
            уходит в единый Gateway Turn API (``/api/v1/dialogue/turn``);
            ядро (AntigonaBrain) решает: разговор, задача, управление,
            уточнение. Бот только отображает прогресс и результат.
            """
            bot_username, bot_id = await self._resolve_bot_info()
            if not should_process_message(message, bot_username, bot_id):
                return

            goal_text = message.text or ""
            if not goal_text.strip():
                return

            # Reply correlation: a [Txxx] tag in the request must open the
            # first paragraph of the reply, even if the core dropped it.
            task_tag = extract_task_tag(goal_text)

            # ── Owner check (Level 1: Telegram ID) ──────────────────────
            from antigona.security.owner_identity import OwnerIdentity
            identity = OwnerIdentity()
            user_id = getattr(message.from_user, "id", 0)
            if not identity.is_owner(user_id):
                logger.info("Access denied for user_id=%d (not owner)", user_id)
                await message.answer(
                    "🚫 Доступ запрещён. Этот бот предназначен только для владельца."
                )
                return

            # Expand @file:, @folder:, @url: references
            expanded_text = expand_refs(goal_text)

            chat_id = message.chat.id
            cid = _generate_correlation_id()
            reply_to_orig = getattr(message.reply_to_message, "message_id", None)
            _current_operation_id: str | None = None

            # ── Operation lifecycle (транспортная презентация) ─────────
            _steered_operation: bool = False
            if reply_to_orig is not None:
                try:
                    active_op = (
                        await self.operation_store.find_active_by_progress_message(
                            chat_id,
                            reply_to_orig,
                        )
                    )
                    if active_op is not None:
                        steered = await self.operation_store.transition_status(
                            active_op.id,
                            OperationState.WAITING_USER,
                            expected_current=active_op.status,
                        )
                        if steered:
                            await self.event_bus.publish(
                                StageChanged(
                                    correlation_id=cid,
                                    operation_id=active_op.id,
                                    stage="WAITING_USER",
                                    description="Корректировка пользователя принята",
                                ),
                            )
                            _current_operation_id = active_op.id
                            _steered_operation = True
                except Exception:
                    logger.exception("Error checking steering for chat=%d", chat_id)

            if not _steered_operation:
                try:
                    operation = await self.operation_store.create(
                        chat_id=chat_id,
                        user_id=user_id,
                        text=goal_text,
                        message_id=message.message_id,
                        reply_to_message_id=reply_to_orig,
                        correlation_id=cid,
                    )
                    await self.event_bus.publish(
                        OperationReceived(
                            correlation_id=cid,
                            operation_id=operation.id,
                            chat_id=chat_id,
                            user_id=user_id,
                            text=goal_text,
                            message_id=message.message_id,
                        ),
                    )
                    _current_operation_id = operation.id
                    await self.event_bus.publish(
                        StageChanged(
                            correlation_id=cid,
                            operation_id=operation.id,
                            stage="CLASSIFYING",
                            step=0,
                            total=1,
                            description="Анализирую запрос",
                        ),
                    )
                except Exception:
                    logger.exception("Failed to create operation for chat=%d", chat_id)
                    _current_operation_id = None

            await self.event_bus.publish(
                MessageReceived(
                    correlation_id=cid,
                    chat_id=chat_id,
                    user_id=user_id,
                    text=goal_text,
                    message_id=message.message_id,
                )
            )

            typing_bot = message.bot
            if typing_bot is None:
                return
            try:
                await typing_bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
            except Exception:
                pass

            # ── Единственный вызов в ядро: Gateway Turn API ─────────────
            try:
                turn = await self._bridge_turn(message, expanded_text, kind="message")
            except BridgeOverflow:
                await message.answer(
                    "⏳ Очередь этого чата заполнена. Повторите сообщение позже.",
                    reply_to_message_id=message.message_id,
                )
                self._update_last_response_time(chat_id)
                return
            except TurnCancelled:
                await self._publish_operation_final(
                    _current_operation_id,
                    "🚫 Сообщение снято из очереди по /stop.",
                    terminal_state=OperationState.CANCELLED,
                )
                self._update_last_response_time(chat_id)
                return
            except AmbiguousTurn:
                # A previous process may already have run this turn; replaying
                # it could execute the same agent work twice.
                await self._publish_operation_final(
                    _current_operation_id,
                    "⚠️ Это сообщение уже обрабатывалось до перезапуска, а "
                    "результат неизвестен. Повторный запуск не выполнен — "
                    "отправьте сообщение заново, если нужен повтор.",
                    terminal_state=OperationState.FAILED,
                )
                self._update_last_response_time(chat_id)
                return
            except Exception as exc:
                logger.warning("Gateway dialogue turn failed for chat=%d: %s", chat_id, exc)
                # Distinguish connection failures from timeouts/HTTP so the
                # user is not told "gateway down" when the LLM is just slow.
                exc_name = type(exc).__name__
                msg = str(exc)
                if (
                    "Connect" in exc_name
                    or "недоступен" in msg.lower()
                    or "connection refused" in msg.lower()
                ):
                    user_text = "⚠️ Gateway временно недоступен. Попробуйте позже."
                    stage = "Gateway временно недоступен. Попробуйте позже."
                elif "Timeout" in exc_name or "истекло" in msg.lower() or "timeout" in msg.lower():
                    user_text = "⏳ Gateway не ответил вовремя (долгий LLM). Повторите запрос."
                    stage = "Таймаут ожидания ответа Gateway/LLM."
                elif "HTTP" in exc_name or "ошибкой" in msg.lower():
                    user_text = "⚠️ Gateway вернул ошибку. Подробности в логе; повторите позже."
                    stage = "Gateway HTTP error."
                else:
                    user_text = "⚠️ Не удалось обработать сообщение. Попробуйте позже."
                    stage = f"Turn failed: {exc_name}."
                await self._publish_operation_stage(
                    _current_operation_id,
                    OperationState.FAILED.value,
                    description=stage,
                )
                await self._publish_operation_final(
                    _current_operation_id,
                    user_text,
                    terminal_state=OperationState.FAILED,
                )
                self._update_last_response_time(chat_id)
                return

            response_type = str(turn.get("response_type") or "conversation").upper()
            reply = str(turn.get("reply") or "").strip()
            flow_id = str(turn.get("flow_id") or "") or None
            requires_approval = bool(turn.get("requires_approval") or False)

            # ── TASK_ACCEPTED: задача создана ядром — ждём терминал ────
            if response_type == "TASK_ACCEPTED" and flow_id:
                if _current_operation_id is not None:
                    try:
                        await self.operation_store.set_flow_id(
                            _current_operation_id,
                            flow_id,
                        )
                    except Exception:
                        logger.exception("Failed to bind flow to operation")
                await self._publish_operation_stage(
                    _current_operation_id,
                    "RUNNING",
                    description=reply or "Задача принята; выполняется.",
                )
                terminal_text, terminal_state, is_done = await self._wait_flow_terminal(
                    flow_id,
                    message,
                    chat_id=chat_id,
                )
                terminal_text = ensure_task_tag_in_first_paragraph(terminal_text, task_tag)
                if requires_approval or terminal_state is OperationState.WAITING_USER:
                    # Карточка уже показала кнопки в _wait_flow_terminal
                    pass
                await self._publish_operation_stage(
                    _current_operation_id,
                    (
                        OperationState.SUCCEEDED.value
                        if is_done
                        else terminal_state.value
                    ),
                    description=terminal_text,
                )
                if not is_done and _current_operation_id is not None:
                    # Durability: a non-successful flow must leave a REAL
                    # last_error on the operation, never SUCCEEDED/None.
                    try:
                        await self.operation_store.set_last_error(
                            _current_operation_id,
                            _safe_result_text(
                                terminal_text,
                                fallback="flow failed",
                                max_length=500,
                            ),
                        )
                    except Exception:
                        logger.exception("Failed to persist flow last_error")
                delivered = await self._publish_operation_final(
                    _current_operation_id,
                    terminal_text,
                    terminal_state=(
                        OperationState.SUCCEEDED if is_done else terminal_state
                    ),
                    artifacts=self._turn_artifacts(turn),
                    completed_action=is_done,
                )
                if delivered:
                    final_receipt = await self._operation_final_receipt(
                        _current_operation_id
                    )
                    if final_receipt is not None:
                        try:
                            await self.binding_repo.save(
                                chat_id=chat_id,
                                telegram_message_id=final_receipt,
                                user_id=None,
                                source_message_id=message.message_id,
                                task_id=flow_id,
                                correlation_id=cid,
                                message_role="assistant",
                                message_kind="task_result",
                            )
                        except Exception:
                            logger.exception("Failed to persist presenter receipt binding")
                self._update_last_response_time(chat_id)
                return

            # ── TASK_RESULT / CONVERSATION / CLARIFICATION / CONTROL ────
            # Phase 3: an empty reply is NEVER success — the core returned no
            # result, so the terminal state is an explicit non-success.
            # Phase 4: technical execution != verified success. A TASK_RESULT is
            # only a success when the core confirmed it (verified=True); a
            # task result that was NOT confirmed must not render as "✅ Готово".
            verified = turn.get("verified")
            is_task_result = response_type == "TASK_RESULT"
            task_not_verified = is_task_result and verified is not True
            # Truth contract: the core reports the REAL tool/step outcome of the
            # turn.  A denied/failed/partial tool run must never render or be
            # recorded as a success, no matter how conversational the reply
            # text looks (a fail-closed denial can arrive as a plain string).
            tool_outcome = str(turn.get("tool_outcome") or "").strip().upper()
            turn_last_error = turn.get("last_error")
            if not isinstance(turn_last_error, str) or not turn_last_error.strip():
                turn_last_error = None
            tool_failed = tool_outcome in ("FAILED", "DENIED", "PARTIAL")
            # Truthfulness: a turn that executed NO tool cannot present a
            # failure.  If a plain conversation reply nonetheless carries
            # fail-closed internals the OWNER did not ask about, it is a
            # stale/echoed failure from an earlier turn — answer neutrally
            # instead of re-presenting the old error as this turn's result.
            # FP-L22: a term the owner used in THIS message («что такое
            # fail-closed?») is a quoted term, not a leak — shared criterion.
            if (
                response_type == "CONVERSATION"
                and not tool_outcome
                and reply
                and internal_marker_leak(reply, expanded_text)
            ):
                logger.info(
                    "Conversation turn replayed a previous failure; "
                    "replaced with a neutral reply (chat=%s)", chat_id,
                )
                reply = _NEUTRAL_STALE_ERROR_REPLY
            # FP-L06: this turn reports NO tool outcome, so nothing backs a
            # claim that an instrument returned an output/error.  A reply that
            # presents one anyway (invented, or a previous diagnosis replayed as
            # current) gets an honest «не вызывала» answer instead of the text.
            elif (
                response_type == "CONVERSATION"
                and not tool_outcome
                and reply
                and ungrounded_tool_output_claim(reply, ())
            ):
                logger.info(
                    "Conversation turn presented an ungrounded tool-output "
                    "claim; replaced with an honest reply (chat=%s)", chat_id,
                )
                reply = _UNGROUNDED_TOOL_OUTPUT_REPLY
            # FP-L06b: the SAME turn's tool SUCCEEDED, yet the reply calls that
            # tool broken.  The «Инструмент `X` выполнен: …» line is rendered
            # from the real result, so the contradiction is unconditionally
            # false — never shown (shared criterion, one place).
            elif (
                response_type == "CONVERSATION"
                and tool_outcome == "SUCCEEDED"
                and reply
                and contradicts_a_successful_tool(reply)
            ):
                logger.info(
                    "Conversation turn claimed a tool that succeeded is "
                    "broken; replaced with an honest reply (chat=%s)", chat_id,
                )
                reply = _UNGROUNDED_TOOL_BREAKAGE_REPLY
            is_refusal = (
                bool(reply)
                and (
                    reply.startswith(("🚫", "❌"))
                    or any(
                        m in reply.casefold()
                        for m in (
                            "действие не выполнено",
                            "действие отклонено",
                            "операция отклонена",
                            "отклонено защитой",
                            "заблокировано политикой",
                            "withheld by safety policy",
                            "refused path",
                            "запрещено",
                            "запрещен",
                            "выходит за разрешённые границы",
                        )
                    )
                )
            )
            is_error = (
                response_type in ("ERROR", "AUTH_REQUIRED")
                or not reply
                or task_not_verified
                or tool_failed
                or is_refusal
            )
            if not reply:
                final_text = (
                    "❌ Пустой результат: ядро не вернуло ответ. "
                    "Техническое выполнение ≠ подтверждённый успех."
                )
            elif tool_failed:
                # Keep the failure honest and actionable while stripping any
                # internal security wording (fencing token / ownership
                # internals) out of the user-visible chat text.
                final_text = _sanitize_user_facing_error(reply)
                if tool_outcome == "PARTIAL":
                    # A multi-step flow that only partly succeeded is NOT a
                    # full success and must be presented as such.
                    final_text = (
                        "⚠️ Выполнено частично: часть шагов не завершилась.\n\n"
                        + final_text
                    )
            else:
                # Keep the core\'s honest text; the terminal state (not the
                # wording) is what distinguishes a confirmed success.
                final_text = reply
            # TTS (voice marker): send the audio as a voice message and strip
            # the voice marker from the visible text.  P1 FALSE_DONE guard:
            # speech.tts success != user task DONE.  If the reply promises a
            # voice artifact but we cannot deliver it (file missing/empty or
            # the Telegram send raises), the terminal state MUST be non-success
            # (FAILED), never SUCCEEDED from the text alone.
            voice_match = re.search(r"⟪voice:([^⟫]+)⟫", final_text.replace(chr(92), "/"))
            voice_delivery_failed = False
            delivered_voice = False
            if voice_match:
                voice_path = voice_match.group(1).strip()
                final_text = (final_text[: voice_match.start()] + final_text[voice_match.end():]).strip()
                delivered_voice = False
                try:
                    from pathlib import Path as _VoicePath
                    vp = _VoicePath(voice_path)
                    if vp.exists() and vp.stat().st_size > 0:
                        from aiogram.types import FSInputFile as _FSInputFile
                        await message.answer_voice(
                            _FSInputFile(str(vp)),
                            reply_to_message_id=message.message_id,
                        )
                        delivered_voice = True
                except Exception:
                    logger.exception("voice send failed for %s", voice_path)
                if not delivered_voice:
                    voice_delivery_failed = True
            if voice_delivery_failed:
                # A promised audio artifact was not delivered — never report DONE.
                is_error = True
                final_text = (
                    final_text
                    + "\n\n❌ Аудио не доставлено: файл озвучки недоступен или отправка не удалась. "
                    + "Генерация ≠ доставка."
                )
            final_text = ensure_task_tag_in_first_paragraph(final_text, task_tag)
            # Durability: a non-successful turn must leave a REAL last_error on
            # the operation, not a SUCCEEDED row with last_error=None.
            if is_error and _current_operation_id is not None:
                try:
                    await self.operation_store.set_last_error(
                        _current_operation_id,
                        _safe_result_text(
                            turn_last_error or final_text,
                            fallback="turn failed",
                            max_length=500,
                        ),
                    )
                except Exception:
                    logger.exception("Failed to persist operation last_error")
            await self._publish_operation_stage(
                _current_operation_id,
                (
                    OperationState.FAILED.value
                    if is_error
                    else OperationState.SUCCEEDED.value
                ),
                description=final_text,
            )
            # A completion header is honest only when a real action finished:
            # a successful tool run, a delivered voice artifact, or a verified
            # task result.  A plain conversational reply gets no header.
            action_completed = (
                not is_error
                and (
                    tool_outcome == "SUCCEEDED"
                    or delivered_voice
                    or is_task_result
                )
            )
            delivered = await self._publish_operation_final(
                _current_operation_id,
                final_text,
                terminal_state=(
                    OperationState.FAILED if is_error else OperationState.SUCCEEDED
                ),
                artifacts=self._turn_artifacts(turn),
                completed_action=action_completed,
            )
            if delivered:
                final_receipt = await self._operation_final_receipt(
                    _current_operation_id
                )
                if final_receipt is not None:
                    try:
                        await self.binding_repo.save(
                            chat_id=chat_id,
                            telegram_message_id=final_receipt,
                            user_id=None,
                            source_message_id=message.message_id,
                            task_id=flow_id,
                            correlation_id=cid,
                            message_role="assistant",
                            message_kind="acknowledgment",
                        )
                    except Exception:
                        logger.exception("Failed to persist acknowledgment binding")
            self._update_last_response_time(chat_id)


    async def _wait_flow_terminal(
        self,
        flow_id: str,
        message: Message,
        *,
        chat_id: int,
    ) -> tuple[str, OperationState, bool]:
        """Дождаться терминального состояния Gateway-задачи.

        Возвращает (текст, OperationState, is_done). При WAITING_APPROVAL
        показывает кнопки одобрения и ждёт решения (поллинг с бюджетом).
        """
        import os as _os

        budget = 30.0
        try:
            budget = float(_os.getenv("ANTIGONA_GATEWAY_TERMINAL_WAIT_SECONDS", "30"))
        except ValueError:
            budget = 30.0
        budget = max(1.0, min(budget, 300.0))

        from antigona.core.control_plane import FlowStatus

        deadline = time.monotonic() + budget
        last_text = "Задача выполняется…"
        last_status: FlowStatus | None = None
        while time.monotonic() < deadline:
            try:
                flow = await self.gateway_client.get_flow(flow_id)
                status = _coerce_flow_status(getattr(flow, "status", None))
            except Exception as exc:
                logger.warning("get_flow failed for %s: %s", flow_id[:12], exc)
                await asyncio.sleep(0.5)
                continue

            if status is None:
                await asyncio.sleep(0.5)
                continue

            # Remember the REAL observed status: the budget may expire while the
            # flow is parked on the owner's decision, and that must be reported
            # as such (never a blanket RUNNING, never silence) — FP-L03d.
            last_status = status

            if status in (FlowStatus.WAITING_APPROVAL, FlowStatus.WAITING_USER):
                try:
                    approvals_list = await self.gateway_client.list_approvals(
                        status="PENDING", limit=100
                    )
                    items = getattr(approvals_list, "items", None)
                    if items is None and isinstance(approvals_list, dict):
                        items = approvals_list.get("items", [])
                    pending = [
                        item for item in (items or [])
                        if str(
                            getattr(item, "task_id", None)
                            or (item.get("task_id") if isinstance(item, dict) else "")
                        ) == flow_id
                    ]
                    if pending:
                        flow_data: dict[str, Any] = {
                            "id": flow_id,
                            "approvals": [
                                {
                                    "id": str(
                                        getattr(pa, "id", None)
                                        or (pa.get("id") if isinstance(pa, dict) else "")
                                    ),
                                    "decision": "PENDING",
                                    "risk_level": str(
                                        getattr(pa, "risk_level", None)
                                        or (pa.get("risk_level") if isinstance(pa, dict) else "MEDIUM")
                                    ),
                                    "reason": str(
                                        getattr(pa, "reason", None)
                                        or (pa.get("reason") if isinstance(pa, dict) else "approval required")
                                    ),
                                }
                                for pa in pending
                            ],
                        }
                        keyboard = make_approval_keyboard(flow_data)
                        if keyboard is not None and flow_id not in self._approval_cards_shown:
                            await message.answer(
                                "⚠️ Требуется одобрение:",
                                reply_markup=keyboard,
                            )
                            self._approval_cards_shown.add(flow_id)
                except Exception as exc:
                    logger.warning(
                        "Failed to render approval for flow %s: %s",
                        flow_id[:12], exc,
                    )
                # Ждём решение — короткий поллинг до конца бюджета
                await asyncio.sleep(1.0)
                continue

            if status in {
                FlowStatus.DONE,
                FlowStatus.FAILED,
                FlowStatus.BLOCKED,
                FlowStatus.CANCELLED,
                FlowStatus.TIMEOUT,
                FlowStatus.POLICY_DENIED,
            }:
                if status is FlowStatus.DONE:
                    # Raw DONE is not success: only the typed result projection
                    # (terminal + success + verified evidence + usable text)
                    # may publish SUCCEEDED. Anything else fails closed.
                    try:
                        result_view = await self.gateway_client.get_result(flow_id)
                    except Exception:
                        logger.exception(
                            "get_result failed for %s — failing closed", flow_id[:12]
                        )
                        return (
                            "Не удалось получить результат задачи.",
                            OperationState.FAILED,
                            False,
                        )
                    safe_text = _safe_result_text(
                        getattr(result_view, "safe_result_text", None)
                        or getattr(result_view, "stdout_preview", None),
                        fallback="",
                        max_length=3500,
                    )
                    artifacts = getattr(result_view, "artifacts", None) or []
                    has_verified_artifact = any(
                        bool(getattr(artifact, "verified", False))
                        for artifact in artifacts
                    )
                    if (
                        bool(getattr(result_view, "terminal", False))
                        and bool(getattr(result_view, "success", False))
                        and has_verified_artifact
                        and bool(safe_text.strip())
                    ):
                        return safe_text, OperationState.SUCCEEDED, True
                    return (
                        safe_text or "Задача завершилась без подтверждённого результата.",
                        OperationState.FAILED,
                        False,
                    )
                if status is FlowStatus.CANCELLED:
                    return "Задача отменена.", OperationState.CANCELLED, False
                if status is FlowStatus.POLICY_DENIED:
                    return "Выполнение отклонено политикой безопасности.", OperationState.FAILED, False
                if status is FlowStatus.BLOCKED:
                    return "Задача заблокирована политикой безопасности.", OperationState.FAILED, False
                if status is FlowStatus.TIMEOUT:
                    return "Задача завершилась по тайм-ауту.", OperationState.FAILED, False
                return (
                    _failure_text_with_reason(
                        "Задача не выполнена.", getattr(flow, "error", None)
                    ),
                    OperationState.FAILED,
                    False,
                )

            last_text = f"Задача выполняется: {status.value}"
            await asyncio.sleep(0.5)

        # The budget expired while the flow is still open.  Report the REAL
        # observed status (never a blanket RUNNING) and an honest text, so the
        # owner is told the task is parked on their decision instead of getting
        # nothing at all (FP-L03d).
        if last_status in (FlowStatus.WAITING_APPROVAL, FlowStatus.WAITING_USER):
            return (
                "Задача ждёт вашего решения (одобрение или уточнение) — "
                "она не завершена.",
                _open_operation_state(last_status),
                False,
            )
        return (
            last_text + " — выполнение продолжается, результат придёт позже.",
            _open_operation_state(last_status),
            False,
        )

    async def _on_dispatcher_startup(self, **_kwargs: Any) -> None:
        """Run once inside the real polling event loop, before the first update.

        Registered via ``self.dp.startup.register(...)`` in ``__init__``.
        """
        await self.startup()

    async def _on_dispatcher_shutdown(self, **_kwargs: Any) -> None:
        """Shutdown hook — закрыть долгоживущие ресурсы."""
        await self.close()

    async def startup(self) -> None:
        """Initialize durable storage and background resources exactly once."""
        if self._started:
            return
        if self._closed:
            raise RuntimeError("TelegramBot cannot be restarted after close")
        await asyncio.to_thread(self.database.create_all)
        # Fail closed before polling: the durable exactly-once ledger is a
        # promise, not an option. An unwritable ledger (e.g. read-only code
        # root without a configured state root) must stop startup loudly.
        ledger = getattr(self.telegram_bridge, "ledger", None)
        ledger_path = getattr(ledger, "_path", None)
        if ledger_path is not None:
            await asyncio.to_thread(ensure_ledger_writable, ledger_path)
        await self._start_presenter()
        if os.getenv("ANTIGONA_ENABLE_STARTUP_RECOVERY", "1") != "0":
            self._startup_recovery_task = asyncio.create_task(self._recover_on_startup())
        self._started = True

    async def close(self) -> None:
        """Release long-lived resources. Idempotent."""
        if self._closed:
            return
        self._closed = True
        if self._startup_recovery_task is not None:
            self._startup_recovery_task.cancel()
            await asyncio.gather(self._startup_recovery_task, return_exceptions=True)
        try:
            await self.telegram_bridge.close()
        except Exception:
            logger.exception("Error stopping Telegram bridge")
        try:
            if getattr(self.operation_presenter, "stop", None) is not None:
                await self.operation_presenter.stop()
        except Exception:
            logger.exception("Error stopping operation presenter")
        # The aiogram Bot owns an aiohttp ClientSession + TCP connector. Not
        # closing them leaks a socket pool per bot instance ("Unclosed client
        # session" at GC time), which is exactly the kind of failure-path leak
        # a long-lived transport must not have.
        try:
            session = getattr(self.bot, "session", None)
            if session is not None and getattr(session, "close", None) is not None:
                await session.close()
        except Exception:
            logger.exception("Error closing Telegram bot session")
        await asyncio.to_thread(self.database.dispose)

    # ── Operation Presenter startup ──────────────────────────────────────

    async def _start_presenter(self) -> None:
        """Subscribe the OperationPresenter to the event bus on startup."""
        try:
            await self.operation_presenter.start()
            logger.info("OperationPresenter started successfully")
        except Exception:
            logger.exception("Failed to start OperationPresenter")

    # ── Restart recovery (Stage 7) ───────────────────────────────────────

    async def _recover_on_startup(self) -> None:
        """Восстановление очередей и bindings после перезапуска."""
        try:
            logger.info("Startup recovery: recovering Telegram bridge queues...")
            if hasattr(self, "telegram_bridge") and hasattr(self.telegram_bridge, "recover_on_startup"):
                recovered = await self.telegram_bridge.recover_on_startup()
                logger.info("Startup recovery: %d queued items active/recovered", recovered)
            logger.info("Startup recovery: database available, bindings lazy-loaded")
        except Exception as exc:
            import sqlite3

            if isinstance(exc, (OSError, sqlite3.Error)):
                # Durable recovery state is unavailable — this is a hard,
                # visible failure, never a silent degradation.
                logger.critical(
                    "Startup recovery failed: durable ledger/queue state is "
                    "unavailable (%s). Refusing to continue without "
                    "exactly-once recovery.",
                    exc,
                )
                raise
            logger.exception("Startup recovery failed")

    # ── Hooks system integration ─────────────────────────────────────────

    async def _on_any_event(self, event: Any) -> None:
        """Wildcard subscriber on the EventBus — fires hooks for every event.

        Registered in ``__init__`` via ``subscribe_any``.
        """
        try:
            await fire_hooks_for_event(self._hook_registry, event)
        except Exception:
            logger.exception("Hook dispatch failed for %s", type(event).__name__)

    # ── Voice/TTS helpers ────────────────────────────────────────────────

    async def _ensure_presented_operation(
        self,
        message: Message,
        existing_operation_id: str | None,
    ) -> str | None:
        """Return the existing operation or create and announce one."""
        if existing_operation_id:
            return existing_operation_id

        try:
            correlation_id = _generate_correlation_id()
            operation = await self.operation_store.create(
                chat_id=message.chat.id,
                user_id=getattr(message.from_user, "id", 0),
                text=message.text or "",
                message_id=message.message_id,
                reply_to_message_id=getattr(
                    message.reply_to_message, "message_id", None
                ),
                correlation_id=correlation_id,
            )
            await self.event_bus.publish(
                OperationReceived(
                    correlation_id=correlation_id,
                    operation_id=operation.id,
                    chat_id=message.chat.id,
                    user_id=getattr(message.from_user, "id", 0),
                    text=operation.text or "",
                    message_id=operation.origin_message_id,
                )
            )
        except Exception:
            logger.exception("Failed to create operation for action execution")
            return None
        return operation.id

    async def _publish_operation_stage(
        self,
        operation_id: str | None,
        stage: str,
        *,
        step: int = 0,
        total: int = 0,
        tool_name: str | None = None,
        description: str | None = None,
    ) -> None:
        """Publish one typed stage event for the presenter-owned bubble."""
        if operation_id is None:
            return
        safe_description = (
            _safe_result_text(
                description,
                fallback="Обновление этапа",
                max_length=512,
            )
            if description is not None
            else None
        )
        safe_tool_name = (
            _safe_result_text(tool_name, fallback="tool", max_length=80)
            if tool_name is not None
            else None
        )
        try:
            await self.event_bus.publish(
                StageChanged(
                    operation_id=operation_id,
                    stage=stage,
                    step=step,
                    total=total,
                    tool_name=safe_tool_name,
                    description=safe_description,
                )
            )
        except Exception:
            logger.exception(
                "Failed to publish stage %s for operation %s",
                stage,
                operation_id[:8],
            )

    async def _publish_tool_progress(
        self,
        operation_id: str | None,
        *,
        tool_name: str,
        status: str,
        preview: str,
    ) -> None:
        if operation_id is None:
            return
        safe_tool_name = _safe_result_text(
            tool_name,
            fallback="tool",
            max_length=80,
        )
        safe_status = _safe_result_text(
            status,
            fallback="RUNNING",
            max_length=160,
        )
        safe_preview = _safe_result_text(
            preview,
            fallback="Результат недоступен",
            max_length=500,
        )
        try:
            await self.event_bus.publish(
                ToolProgress(
                    operation_id=operation_id,
                    tool_name=safe_tool_name,
                    status=safe_status,
                    stdout_preview=safe_preview,
                )
            )
        except Exception:
            logger.exception(
                "Failed to publish tool progress for operation %s",
                operation_id[:8],
            )

    async def _publish_operation_final(
        self,
        operation_id: str | None,
        text: str,
        *,
        terminal_state: OperationState,
        artifacts: tuple[str, ...] = (),
        completed_action: bool = False,
    ) -> bool:
        """Await presenter delivery and acknowledge only a real Telegram receipt.

        ``completed_action`` is True only when this turn actually completed a
        real action; a plain conversational reply leaves it False so no
        completion header is rendered.
        """
        if operation_id is None:
            return False
        # The presenter splits long finals across ordered messages, so the
        # transport no longer has to truncate at one Telegram message.
        safe_text = _safe_result_text(
            text,
            fallback="Результат недоступен",
            max_length=MAX_FINAL_TEXT,
        )
        try:
            receipt = await self.operation_presenter.deliver_final(
                FinalResponseReady(
                    operation_id=operation_id,
                    text=safe_text,
                    parse_mode="HTML",
                    terminal_state=terminal_state.value,
                    artifacts=artifacts,
                    completed_action=completed_action,
                )
            )
        except Exception:
            logger.exception(
                "Failed to publish FinalResponseReady for operation %s",
                operation_id[:8],
            )
            return False
        return (
            isinstance(receipt, int)
            and not isinstance(receipt, bool)
            and receipt > 0
        )

    async def _operation_final_receipt(self, operation_id: str | None) -> int | None:
        """Read the presenter's persisted delivery manifest."""
        if operation_id is None:
            return None
        try:
            operation = await self.operation_store.get(operation_id)
        except Exception:
            logger.exception("Failed to load operation final receipt")
            return None
        if operation is None:
            return None
        receipts = getattr(operation, "final_message_ids", None) or []
        for receipt in receipts:
            if isinstance(receipt, int) and not isinstance(receipt, bool) and receipt > 0:
                return receipt
        return None


def main() -> None:
    """Entry point: start the bot polling loop."""
    logging.basicConfig(level=logging.INFO)
    try:
        lock_file = acquire_pid_lock()
    except PidLockError as exc:
        # FAIL CLOSED with an actionable error. A setup failure (read-only code
        # root / EROFS, EACCES/EPERM, missing pid dir) is NOT a concurrent
        # instance — claiming one would silently mask a broken deployment.
        # The detail is our own constructed lock-path/errno text (never a remote
        # payload), and is deliberately not interpolated as the raw exception.
        detail = str(exc)
        logging.error("Cannot establish the Antigona bot instance lock: %s", detail)
        print(f"Cannot establish the Antigona bot instance lock: {detail}", file=sys.stderr)
        raise SystemExit(2) from exc
    if lock_file is None:
        logging.error(
            "Another instance of Antigona bot is already running. Exiting to prevent 409 conflict."
        )
        print("Another instance of Antigona bot is already running. Exiting.")
        return

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token or token == "dummy-token":
        print("TELEGRAM_BOT_TOKEN environment variable not set or invalid. Exiting.")
        return
    gateway_url = os.getenv("ANTIGONA_GATEWAY_URL", "http://127.0.0.1:8090")
    gateway_token = os.getenv("ANTIGONA_GATEWAY_TOKEN", "gateway-token")

    bot_app = TelegramBot(
        token=token, gateway_url=gateway_url, gateway_token=gateway_token
    )

    async def _drive() -> None:
        # Close session repo on the SAME loop that drives polling, before
        # asyncio.run() tears the loop down. If polling returns (or raises)
        # with the aiosqlite sessions connection still open, the non-daemon
        # worker thread blocks threading._shutdown at interpreter exit and
        # the process hangs (root cause of the previously-stuck bot). The
        # dispatcher shutdown hook covers the normal path; this finally
        # covers exceptions / early returns. close() is idempotent.
        try:
            await bot_app.dp.start_polling(bot_app.bot)
        finally:
            await bot_app.close()

    asyncio.run(_drive())


if __name__ == "__main__":
    main()
