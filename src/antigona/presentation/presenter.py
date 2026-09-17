"""Telegram presentation for durable operation progress and terminal delivery.

The presenter is the sole owner of progress/final Telegram messages.  A final
is acknowledged only after ``sendMessage``, durable manifest persistence, and
a guarded terminal-state commit all succeed.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from antigona.channels.telegram.bridge import (
    TELEGRAM_TEXT_LIMIT,
    AttachmentRejected,
    ValidatedArtifact,
    resolve_outbound_artifact,
    split_telegram_html,
    utf16_length,
)
from antigona.durable.operation_models import OperationState, StateMachine
from antigona.durable.operation_store import OperationStore
from antigona.events.bus import EventBus
from antigona.events.event_types import (
    BaseEvent,
    DeliveryConfirmed,
    FinalResponseReady,
    OperationReceived,
    StageChanged,
    ToolProgress,
)
from antigona.result_safety import sanitize_result_text

logger = logging.getLogger(__name__)

#: Upper bound on a final answer before the transport truncates it.  Long
#: answers are *split* across ordered messages rather than silently cut, so
#: this is a sanity ceiling rather than a display limit.
MAX_FINAL_TEXT = 32_000

#: Reserve for the ``<pre>``/header wrapper added around every body chunk.
_CHUNK_WRAPPER_RESERVE = 32

_STAGE_EMOJI: dict[str, str] = {
    "RECEIVED": "📥",
    "CLASSIFYING": "🧠",
    "PLANNING": "📋",
    "RUNNING": "⚙️",
    "WAITING_TOOL": "🔧",
    "VALIDATING": "🔍",
    "FINALIZING": "📝",
    "WAITING_USER": "💬",
    "ERROR_RECOVERY": "⚠️",
    "SUCCEEDED": "✅",
    "FAILED": "❌",
    "CANCELLED": "🚫",
    "planning": "📋",
    "executing": "⚙️",
    "reviewing": "🔍",
    "waiting": "⏳",
    "done": "✅",
    "failed": "❌",
    "cancelled": "🚫",
}

_TOOL_EMOJI: dict[str, str] = {
    "web_search": "🌐",
    "code": "💻",
    "shell": "🖥️",
    "file": "📁",
    "read": "📖",
    "write": "✏️",
    "think": "🧠",
    "default": "🔧",
}

# Result redaction handles credential values.  This additional presentation
# guard removes path-shaped references to protected credential stores.
_SENSITIVE_PATH_RE = re.compile(
    r"(?i)(?<![\w.-])(?:"
    r"(?:\.{0,2}/)?[^\s<>&]*(?:\.env(?:\.[^\s<>&]*)?"
    r"|credentials?|secrets?|vaults?|id_(?:rsa|dsa|ecdsa|ed25519)"
    r"|[^\s<>&]*\.(?:pem|key|p12|pfx|kdbx))"
    r")(?![\w.-])"
)


def _get_message_id(data: Any) -> int | None:
    """Return only a real positive Telegram progress message ID."""
    if data is None:
        return None
    value = (
        data.get("progress_message_id")
        if isinstance(data, dict)
        else getattr(data, "progress_message_id", None)
    )
    return value if isinstance(value, int) and value > 0 else None


def _get_origin_message_id(data: Any) -> int | None:
    if data is None:
        return None
    value = (
        data.get("origin_message_id")
        if isinstance(data, dict)
        else getattr(data, "origin_message_id", None)
    )
    if isinstance(value, int) and value > 0:
        return value
    value = (
        data.get("message_id")
        if isinstance(data, dict)
        else getattr(data, "message_id", None)
    )
    return value if isinstance(value, int) and value > 0 else None


def _get_chat_id(data: Any) -> int | None:
    if data is None:
        return None
    value = data.get("chat_id") if isinstance(data, dict) else getattr(data, "chat_id", None)
    return value if isinstance(value, int) else None


def _get_status(data: Any) -> OperationState | None:
    if data is None:
        return None
    raw = data.get("status") if isinstance(data, dict) else getattr(data, "status", None)
    if raw is None:
        return None
    try:
        return OperationState(str(raw).upper())
    except ValueError:
        logger.warning("Unknown operation status for presentation")
        return None


def _get_final_message_ids(data: Any) -> list[int]:
    if data is None:
        return []
    raw = (
        data.get("final_message_ids", [])
        if isinstance(data, dict)
        else getattr(data, "final_message_ids", [])
    )
    return [value for value in (raw or []) if isinstance(value, int) and value > 0]


def _sent_message_id(data: Any) -> int | None:
    """Extract a real Telegram receipt from a successful send response."""
    value = data.get("message_id") if isinstance(data, dict) else getattr(data, "message_id", None)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _terminal_state(value: str | OperationState) -> OperationState | None:
    try:
        state = value if isinstance(value, OperationState) else OperationState(str(value).upper())
    except ValueError:
        return None
    return state if StateMachine.is_terminal(state) else None


def _safe_untrusted(value: object | None, *, max_length: int) -> str:
    sanitized = sanitize_result_text(value, max_length=max_length, preserve_newlines=True) or ""
    sanitized = _SENSITIVE_PATH_RE.sub("\u2026", sanitized)
    return sanitized


#: Telegram reports a broken ``parse_mode`` payload as a 400 whose description
#: mentions entity parsing.  Anything else is a transport problem, not a
#: formatting problem, and must not trigger the plain-text downgrade.
_ENTITY_ERROR_MARKERS = (
    "parse entities",
    "can't parse",
    "cant parse",
    "unsupported start tag",
    "unclosed start tag",
    "entity",
)


def _is_entity_error(exc: BaseException) -> bool:
    message = str(exc).lower()
    return any(marker in message for marker in _ENTITY_ERROR_MARKERS)


def _default_artifact_roots() -> tuple[Path, ...]:
    """Roots Antigona may deliver from: its workspace and its download area."""
    from antigona.core import paths

    roots: list[Path] = []
    for candidate in (paths.workspace_dir(), paths.downloads_dir()):
        try:
            roots.append(candidate.resolve())
        except OSError:  # pragma: no cover - unreadable root
            continue
    return tuple(roots)


class OperationPresenter:
    """Deliver exactly one progress bubble and at-most-once terminal final."""

    def __init__(
        self,
        bot: Any,
        event_bus: EventBus,
        operation_store: OperationStore,
        edit_debounce_seconds: float = 1.5,
        artifact_roots: tuple[Path, ...] | None = None,
    ) -> None:
        self._bot = bot
        self._event_bus = event_bus
        self._store = operation_store
        self._debounce = edit_debounce_seconds
        self._last_edit_time: dict[int, float] = {}
        self._unsubscribers: list[Callable[[], None]] = []
        self._operation_locks: dict[str, asyncio.Lock] = {}
        self._running = False
        # The presenter is the only component that puts bytes on the wire, so
        # it owns the allow-list rather than trusting whoever named the file.
        self._artifact_roots: tuple[Path, ...] = (
            artifact_roots if artifact_roots is not None else _default_artifact_roots()
        )

    def _lock_for(self, operation_id: str) -> asyncio.Lock:
        lock = self._operation_locks.get(operation_id)
        if lock is None:
            lock = asyncio.Lock()
            self._operation_locks[operation_id] = lock
        return lock

    async def start(self) -> None:
        """Install idempotent typed-event subscriptions."""
        if self._running:
            return
        subscriptions: list[tuple[type[BaseEvent], Any]] = [
            (OperationReceived, self._on_operation_received),
            (StageChanged, self._on_stage_changed),
            (ToolProgress, self._on_tool_progress),
            (FinalResponseReady, self._on_final_response_ready),
            (DeliveryConfirmed, self._on_delivery_confirmed),
        ]
        for event_class, handler in subscriptions:
            self._unsubscribers.append(self._event_bus.subscribe(event_class, handler))
        self._running = True
        logger.info("OperationPresenter started with %d subscriptions", len(subscriptions))

    async def stop(self) -> None:
        for unsubscribe in self._unsubscribers:
            unsubscribe()
        self._unsubscribers.clear()
        self._last_edit_time.clear()
        self._operation_locks.clear()
        self._running = False
        logger.info("OperationPresenter stopped")

    async def _on_operation_received(self, event: OperationReceived) -> None:
        """Claim, send, then persist one progress message."""
        async with self._lock_for(event.operation_id):
            try:
                claimed = await self._store.claim_progress_delivery(event.operation_id)
            except Exception:
                logger.exception(
                    "Could not claim progress delivery for operation %s",
                    event.operation_id[:8],
                )
                return
            if not claimed:
                return

            try:
                send_kwargs: dict[str, Any] = {
                    "chat_id": event.chat_id,
                    "text": self._render_operation_received(event),
                    "parse_mode": "HTML",
                }
                if event.message_id > 0:
                    send_kwargs["reply_to_message_id"] = event.message_id
                sent = await self._bot.send_message(**send_kwargs)
            except Exception:
                # The durable sentinel remains. Telegram send failures can be
                # ambiguous, so replay intentionally prefers no duplicate.
                logger.exception(
                    "Progress send failed for operation %s",
                    event.operation_id[:8],
                )
                return

            receipt = _sent_message_id(sent)
            if receipt is None:
                logger.error(
                    "Progress send returned no receipt for operation %s",
                    event.operation_id[:8],
                )
                return

            try:
                persisted = await self._store.save_progress_message_id(
                    event.operation_id,
                    receipt,
                )
            except Exception:
                logger.exception(
                    "Could not persist progress receipt for operation %s",
                    event.operation_id[:8],
                )
                return
            if not persisted:
                logger.error(
                    "Lost progress receipt CAS for operation %s",
                    event.operation_id[:8],
                )

    async def _on_stage_changed(self, event: StageChanged) -> None:
        async with self._lock_for(event.operation_id):
            try:
                target = OperationState(event.stage.upper())
            except ValueError:
                target = None
            if target is not None and not StateMachine.is_terminal(target):
                await self._transition_operation(event.operation_id, target)
            await self._debounced_edit(event.operation_id, event)

    async def _on_tool_progress(self, event: ToolProgress) -> None:
        async with self._lock_for(event.operation_id):
            await self._debounced_edit(event.operation_id, event)

    async def _on_final_response_ready(self, event: FinalResponseReady) -> None:
        """EventBus wrapper; producer-facing acknowledgements use ``deliver_final``."""
        await self.deliver_final(event)

    async def deliver_final(self, event: FinalResponseReady) -> int | None:
        """Deliver and commit a final, returning its real Telegram receipt.

        ``None`` means delivery is unconfirmed.  Publication on EventBus is not
        used as acknowledgement.  The durable claim is intentionally acquired
        before ``sendMessage``; a process crash between those operations can
        lose a final but cannot replay it blindly.
        """
        intended = _terminal_state(event.terminal_state)
        if intended is None:
            logger.error(
                "Rejected nonterminal final outcome for operation %s",
                event.operation_id[:8],
            )
            return None

        async with self._lock_for(event.operation_id):
            try:
                data = await self._store.get(event.operation_id)
            except Exception:
                logger.exception("Could not load operation before final delivery")
                return None
            if data is None:
                return None

            existing_ids = _get_final_message_ids(data)
            if existing_ids:
                receipt = existing_ids[0]
                if not await self._commit_terminal(event.operation_id, intended):
                    return None
                await self._cleanup_progress(event.operation_id)
                return receipt

            try:
                claim = await self._store.claim_final_delivery(event.operation_id)
            except Exception:
                logger.exception(
                    "Could not claim final delivery for operation %s",
                    event.operation_id[:8],
                )
                return None

            if claim.existing_message_id is not None:
                if not await self._commit_terminal(event.operation_id, intended):
                    return None
                await self._cleanup_progress(event.operation_id)
                return claim.existing_message_id
            if not claim.claimed:
                return None

            chat_id = _get_chat_id(data)
            if chat_id is None:
                return None
            origin_message_id = _get_origin_message_id(data)

            receipts = await self._send_final_chunks(
                event, intended, chat_id, reply_to_message_id=origin_message_id
            )
            if not receipts:
                return None
            receipt = receipts[0]

            try:
                for message_id in receipts:
                    await self._store.add_final_message_id(
                        event.operation_id,
                        message_id,
                    )
            except Exception:
                # Manifest persistence is part of the commit point.  Keep the
                # operation nonterminal and retain progress for diagnosis.
                logger.exception(
                    "Final manifest write failed for operation %s",
                    event.operation_id[:8],
                )
                return None

            artifact_receipts = await self._send_artifacts(
                event, chat_id, reply_to_message_id=origin_message_id
            )
            for message_id in artifact_receipts:
                try:
                    await self._store.add_final_message_id(
                        event.operation_id,
                        message_id,
                    )
                except Exception:
                    logger.exception("Artifact manifest write failed")

            if not await self._commit_terminal(event.operation_id, intended):
                return None
            await self._cleanup_progress(event.operation_id)

            if intended == OperationState.SUCCEEDED:
                try:
                    from antigona.input_pipeline.binding_repository import BindingRepository
                    from antigona.memory.self_learning import SelfLearningTool

                    binding_repo = BindingRepository(self._store._db)
                    task_id = event.operation_id
                    bindings = await binding_repo.get_by_task_id(task_id)

                    message_ids = []
                    user_inputs = []
                    for b in bindings:
                        if b.message_role == "user":
                            message_ids.append(b.telegram_message_id)
                            txt = b.edited_text or b.original_text
                            if txt:
                                user_inputs.append(txt)

                    sl_tool = SelfLearningTool()
                    candidates = await sl_tool.extract_candidates(
                        event.text,
                        task_id=task_id,
                        message_ids=message_ids,
                    )
                    for cand in candidates:
                        # Record the candidate for the audit trail and for an
                        # explicit later ``/learn`` — but NEVER push an
                        # unsolicited system proposal into the owner chat.  The
                        # owner did not ask to persist a rule; an internal
                        # affordance must not leak into the conversation.
                        # Persisting is strictly opt-in via the ``/learn``
                        # command, which reads these stored candidates.
                        await sl_tool.store_learning(cand)
                except Exception:
                    logger.exception("Failed to run self learning on final delivery")

            return receipt

    # ── Final text delivery ──────────────────────────────────────────────

    async def _send_final_chunks(
        self,
        event: FinalResponseReady,
        intended: OperationState,
        chat_id: int,
        reply_to_message_id: int | None = None,
    ) -> list[int]:
        """Send the final as ordered chunks; return every real receipt.

        Telegram counts UTF-16 code units, so a long answer is split on that
        boundary (never through an HTML entity) and delivered in order.  A
        single entity-parse rejection downgrades the *whole* delivery to plain
        text exactly once — there is no retry loop, and the runtime is never
        re-entered because of a formatting problem.
        """
        chunks = self._render_final_chunks(event, intended)
        plain = self._render_final_chunks(event, intended, plain=True)
        receipts: list[int] = []
        parse_mode: str | None = "HTML"
        fallback_used = False

        for index, chunk in enumerate(chunks):
            payload = chunk if parse_mode else plain[index]
            send_kwargs: dict[str, Any] = {
                "chat_id": chat_id,
                "text": payload,
                "parse_mode": parse_mode,
            }
            if reply_to_message_id is not None:
                send_kwargs["reply_to_message_id"] = reply_to_message_id
            try:
                sent = await self._bot.send_message(**send_kwargs)
            except Exception as exc:
                if parse_mode and not fallback_used and _is_entity_error(exc):
                    fallback_used = True
                    parse_mode = None
                    try:
                        fallback_kwargs: dict[str, Any] = {
                            "chat_id": chat_id,
                            "text": plain[index],
                            "parse_mode": None,
                        }
                        if reply_to_message_id is not None:
                            fallback_kwargs["reply_to_message_id"] = reply_to_message_id
                        sent = await self._bot.send_message(**fallback_kwargs)
                    except Exception:
                        logger.exception(
                            "Plain-text fallback failed for operation %s",
                            event.operation_id[:8],
                        )
                        return receipts
                else:
                    logger.exception(
                        "Final send failed for operation %s (chunk %d/%d)",
                        event.operation_id[:8],
                        index + 1,
                        len(chunks),
                    )
                    return receipts

            receipt = _sent_message_id(sent)
            if receipt is None:
                logger.error(
                    "Final send returned no receipt for operation %s",
                    event.operation_id[:8],
                )
                return receipts
            receipts.append(receipt)

        if len(receipts) != len(chunks):  # pragma: no cover - defensive
            logger.error(
                "Partial final delivery for operation %s: %d/%d",
                event.operation_id[:8],
                len(receipts),
                len(chunks),
            )
        return receipts

    # ── Artifact delivery ────────────────────────────────────────────────

    async def _send_artifacts(
        self,
        event: FinalResponseReady,
        chat_id: int,
        reply_to_message_id: int | None = None,
    ) -> list[int]:
        """Validate then deliver typed artifacts in order after the final text.

        Validation is the presenter's job because the presenter is the only
        component allowed to put bytes on the wire; a caller cannot bypass it
        by handing over a path that was checked somewhere else.
        """
        requested = getattr(event, "artifacts", ()) or ()
        if not requested:
            return []

        validated: list[ValidatedArtifact] = []
        for raw in requested:
            try:
                validated.append(
                    resolve_outbound_artifact(Path(raw), self._artifact_roots)
                )
            except AttachmentRejected as exc:
                # Never echo the rejected path back to the chat.
                logger.warning("Artifact rejected before delivery: %s", exc)

        receipts: list[int] = []
        for artifact in validated:
            lower_name = artifact.name.lower()
            sent = None
            try:
                if lower_name.endswith((".png", ".jpg", ".jpeg", ".webp")) and hasattr(self._bot, "send_photo"):
                    photo_kwargs: dict[str, Any] = {
                        "chat_id": chat_id,
                        "photo": self._input_file(artifact),
                    }
                    if reply_to_message_id is not None:
                        photo_kwargs["reply_to_message_id"] = reply_to_message_id
                    try:
                        sent = await self._bot.send_photo(**photo_kwargs)
                    except Exception:
                        sent = None

                elif lower_name.endswith((".ogg", ".mp3", ".wav", ".opus", ".m4a")) and hasattr(self._bot, "send_voice"):
                    voice_kwargs: dict[str, Any] = {
                        "chat_id": chat_id,
                        "voice": self._input_file(artifact),
                    }
                    if reply_to_message_id is not None:
                        voice_kwargs["reply_to_message_id"] = reply_to_message_id
                    try:
                        sent = await self._bot.send_voice(**voice_kwargs)
                    except Exception:
                        sent = None

                if sent is None:
                    doc_kwargs: dict[str, Any] = {
                        "chat_id": chat_id,
                        "document": self._input_file(artifact),
                    }
                    if reply_to_message_id is not None:
                        doc_kwargs["reply_to_message_id"] = reply_to_message_id
                    sent = await self._bot.send_document(**doc_kwargs)

            except Exception:
                logger.exception(
                    "Artifact delivery failed for operation %s",
                    event.operation_id[:8],
                )
                break
            receipt = _sent_message_id(sent)
            if receipt is not None:
                receipts.append(receipt)
        return receipts

    @staticmethod
    def _input_file(artifact: ValidatedArtifact) -> Any:
        """Wrap already-read bytes so the file is never re-opened by path."""
        try:
            from aiogram.types import BufferedInputFile
        except Exception:  # pragma: no cover - aiogram always present in prod
            return artifact.data
        return BufferedInputFile(artifact.data, filename=artifact.name)

    async def _on_delivery_confirmed(self, event: DeliveryConfirmed) -> None:
        """Compatibility path for a transport that already performed the send."""
        intended = _terminal_state(event.terminal_state)
        if intended is None or event.final_message_id <= 0:
            return
        async with self._lock_for(event.operation_id):
            try:
                data = await self._store.get(event.operation_id)
            except Exception:
                logger.exception("Could not load confirmed delivery operation")
                return
            if data is None:
                return
            if event.final_message_id not in _get_final_message_ids(data):
                try:
                    await self._store.add_final_message_id(
                        event.operation_id,
                        event.final_message_id,
                    )
                except Exception:
                    logger.exception(
                        "Confirmed delivery manifest write failed for operation %s",
                        event.operation_id[:8],
                    )
                    return
            if not await self._commit_terminal(event.operation_id, intended):
                return
            await self._cleanup_progress(event.operation_id)

    async def _commit_terminal(
        self,
        operation_id: str,
        intended: OperationState,
    ) -> bool:
        try:
            data = await self._store.get(operation_id)
        except Exception:
            logger.exception("Could not load operation for terminal commit")
            return False
        current = _get_status(data)
        if current is None:
            return False
        if current is intended:
            return True
        if StateMachine.is_terminal(current):
            return False

        if intended is OperationState.SUCCEEDED and current is not OperationState.FINALIZING:
            if not await self._transition_operation(
                operation_id,
                OperationState.FINALIZING,
                expected_current=current,
            ):
                return False
            current = OperationState.FINALIZING

        return await self._transition_operation(
            operation_id,
            intended,
            expected_current=current,
        )

    async def _cleanup_progress(self, operation_id: str) -> None:
        try:
            data = await self._store.get(operation_id)
        except Exception:
            logger.exception("Could not load operation for progress cleanup")
            return
        message_id = _get_message_id(data)
        chat_id = _get_chat_id(data)
        if message_id is None or chat_id is None:
            return
        try:
            claimed = await self._store.claim_progress_cleanup(operation_id, message_id)
        except Exception:
            logger.exception("Could not claim progress cleanup")
            return
        if not claimed:
            return
        try:
            await self._bot.delete_message(chat_id=chat_id, message_id=message_id)
        except Exception:
            logger.debug(
                "Progress cleanup failed after terminal commit for operation %s",
                operation_id[:8],
            )

    async def _transition_operation(
        self,
        operation_id: str,
        target: OperationState,
        *,
        expected_current: OperationState | None = None,
    ) -> bool:
        try:
            if expected_current is None:
                data = await self._store.get(operation_id)
                current = _get_status(data)
                if current is None:
                    return False
                expected_current = current
            return await self._store.transition_status(
                operation_id,
                target,
                expected_current=expected_current,
            )
        except Exception:
            logger.exception(
                "Operation transition failed for operation %s",
                operation_id[:8],
            )
            return False

    async def _debounced_edit(
        self,
        operation_id: str,
        event: StageChanged | ToolProgress,
    ) -> None:
        try:
            data = await self._store.get(operation_id)
        except Exception:
            logger.exception("Could not load operation for progress edit")
            return
        chat_id = _get_chat_id(data)
        message_id = _get_message_id(data)
        if chat_id is None or message_id is None:
            return
        status = _get_status(data)
        if status is None or StateMachine.is_terminal(status):
            return

        elapsed = time.monotonic() - self._last_edit_time.get(chat_id, 0.0)
        if elapsed < self._debounce:
            await asyncio.sleep(self._debounce - elapsed)

        text = (
            self._render_stage_changed(event)
            if isinstance(event, StageChanged)
            else self._render_tool_progress(event)
        )
        try:
            await self._bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                parse_mode="HTML",
            )
            self._last_edit_time[chat_id] = time.monotonic()
        except Exception as exc:
            exc_name = type(exc).__name__
            if "NotModified" in exc_name:
                self._last_edit_time[chat_id] = time.monotonic()
                return
            if "NotFound" in exc_name or "MessageToEditNotFound" in exc_name:
                return
            logger.warning(
                "Progress edit failed for operation %s (%s)",
                operation_id[:8],
                exc_name,
            )

    @staticmethod
    def _render_operation_received(event: OperationReceived) -> str:
        title = _safe_untrusted(event.text or "Операция", max_length=256)
        return f"🎯 <b>{title}</b>\n⏳ Начинаем выполнение..."

    @staticmethod
    def _render_stage_changed(event: StageChanged) -> str:
        stage = event.stage
        stage_name = _safe_untrusted(
            stage.replace("_", " ").title(),
            max_length=64,
        )
        emoji = _STAGE_EMOJI.get(stage, _STAGE_EMOJI.get(stage.upper(), "🔄"))
        parts = [f"{emoji} <b>{stage_name}</b>"]
        if event.description:
            parts.append(f"\n{_safe_untrusted(event.description, max_length=512)}")
        return "".join(parts)

    @staticmethod
    def _render_tool_progress(event: ToolProgress) -> str:
        tool_name = _safe_untrusted(
            event.tool_name.replace("_", " ").title(),
            max_length=80,
        )
        emoji = _TOOL_EMOJI.get(event.tool_name, _TOOL_EMOJI["default"])
        parts = [f"{emoji} <b>{tool_name}</b>"]
        if event.status:
            parts.append(f"\n{_safe_untrusted(event.status, max_length=160)}")
        if event.stdout_preview:
            preview = _safe_untrusted(event.stdout_preview, max_length=500)
            parts.append(f"\n<code>{preview}</code>")
        return "".join(parts)

    @staticmethod
    def _final_header(
        status: OperationState,
        *,
        plain: bool,
        completed_action: bool = False,
    ) -> str:
        """Completion header — only for a genuinely completed action.

        Failure and cancellation are always labelled honestly.  A successful
        terminal state that completed NO real action (a plain conversational
        reply) renders NO header: a canned "✅ Готово" on every message is noise
        and dilutes the header's meaning.
        """
        if status is OperationState.FAILED:
            title, label = "❌", "Не выполнено"
        elif status is OperationState.CANCELLED:
            title, label = "🚫", "Отменено"
        elif completed_action:
            title, label = "✅", "Готово"
        else:
            return ""
        return f"{title} {label}" if plain else f"{title} <b>{label}</b>"

    @classmethod
    def _render_final_chunks(
        cls,
        event: FinalResponseReady,
        status: OperationState,
        *,
        plain: bool = False,
    ) -> list[str]:
        """Render the final as one or more messages, each within Telegram's cap."""
        body = _safe_untrusted(event.text, max_length=MAX_FINAL_TEXT)
        # Refusal / blocked guard (W6b-4): an action that was refused or blocked must never receive ✅ Готово
        is_refusal_body = bool(body) and (
            body.startswith(("🚫", "❌"))
            or any(
                m in body.casefold()
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
        completed_action = (
            event.completed_action
            and not is_refusal_body
            and status is OperationState.SUCCEEDED
        )
        effective_status = (
            OperationState.FAILED
            if (is_refusal_body and status is not OperationState.CANCELLED)
            else status
        )
        header = cls._final_header(
            effective_status,
            plain=plain,
            completed_action=completed_action,
        )
        if not body:
            return [header] if header else ["..."]

        budget = TELEGRAM_TEXT_LIMIT - utf16_length(header) - _CHUNK_WRAPPER_RESERVE
        pieces = split_telegram_html(body, limit=max(256, budget))
        chunks: list[str] = []
        for index, piece in enumerate(pieces):
            if plain:
                rendered = html.unescape(piece)
                chunks.append(
                    f"{header}\n\n{rendered}" if index == 0 and header else rendered
                )
            else:
                prefix = f"{header}\n\n" if index == 0 and header else ""
                chunks.append(f"{prefix}<pre>{piece}</pre>")
        return chunks

    @classmethod
    def _render_final_response(
        cls,
        event: FinalResponseReady,
        status: OperationState,
    ) -> str:
        """Single-message rendering — retained for callers that need one string."""
        return cls._render_final_chunks(event, status)[0]


def progress_bar(percent: int, width: int = 12) -> str:
    """Render a bounded visual progress bar."""
    filled = max(0, min(width, round(percent / 100 * width)))
    return "▰" * filled + "▱" * (width - filled)
