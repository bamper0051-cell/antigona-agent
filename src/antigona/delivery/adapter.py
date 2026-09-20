from __future__ import annotations

import html
import json
import urllib.request
from dataclasses import dataclass
from typing import Any, Protocol, cast

from .errors import DeliveryConfigError, DeliveryProviderRejected
from .readback import (
    READ_BACK_SEND_ACK,
    READ_BACK_UNSUPPORTED,
    DeliveryOutcome,
    TransmissionProbe,
)

TELEGRAM_TEXT_LIMIT = 4096
TELEGRAM_TRUNCATION_MARKER = "...[truncated]"


def _telegram_utf16_units(text: str) -> int:
    """Return Telegram's message length unit count (UTF-16 code units)."""

    return sum(2 if ord(character) > 0xFFFF else 1 for character in text)


def _truncate_telegram_text(text: str) -> str:
    """Bound plain text without splitting an astral Unicode code point."""

    if _telegram_utf16_units(text) <= TELEGRAM_TEXT_LIMIT:
        return text

    marker_units = _telegram_utf16_units(TELEGRAM_TRUNCATION_MARKER)
    budget = TELEGRAM_TEXT_LIMIT - marker_units
    prefix: list[str] = []
    used = 0
    for character in text:
        units = 2 if ord(character) > 0xFFFF else 1
        if used + units > budget:
            break
        prefix.append(character)
        used += units
    return "".join(prefix) + TELEGRAM_TRUNCATION_MARKER


@dataclass(frozen=True)
class ProgressEvent:
    task_id: str
    session_id: str
    correlation_id: str
    step_id: str | None
    status: str
    message: str
    #: ``"transition"`` marks one INTERNAL state transition (RECEIVED → QUEUED →
    #: … → VERIFYING); anything else (``"result"``) is an owner-facing terminal
    #: message. Chat adapters collapse the internal transitions into ONE editable
    #: progress bubble instead of emitting one message per transition (FP-L07).
    #: The default keeps every pre-existing call site meaning "deliver a normal
    #: message", so no caller loses its message by omission.
    event_type: str = "result"


class DeliveryAdapter(Protocol):
    name: str

    def deliver(self, event: ProgressEvent, idempotency_key: str) -> bool:
        """Return ``True`` when the event was actually transmitted to the external
        channel, ``False`` when the dispatch was only simulated (mock runtime).
        A raised exception means the delivery failed and may be retried.

        This boolean is a *transmission* signal only: it never means the
        recipient read the message. An adapter may additionally implement the
        optional ``deliver_outcome(event, idempotency_key) -> DeliveryOutcome``
        method (:class:`~antigona.delivery.readback.DeliveryOutcome`) to report
        the provider message identifier and a read-back level; callers that only
        need the historical boolean use this method unchanged.
        """
        ...

    # OPTIONAL hook — deliberately NOT a required protocol member.
    #
    # An adapter that can ask its provider "do you still have this message?"
    # implements ``probe_message(provider_message_id) -> bool | None``
    # (:class:`ProbeCapable` below): ``True`` the provider confirmed the message,
    # ``False`` the provider explicitly did not find it, ``None`` it cannot be
    # asked. The *default* for every adapter is "cannot probe" (``None``), which
    # :func:`adapter_probe` returns for an adapter without the hook.
    #
    # It is spelled out here rather than declared as a member because a required
    # protocol member — even one carrying a default body — is still enforced by
    # ``mypy --strict`` for structural subtyping, so declaring it would break
    # every adapter that does not implement it (Telegram, Discord, Slack, …).


class ProbeCapable(Protocol):
    """Optional adapter capability: read a message back from the provider.

    ``probe_message(provider_message_id)`` returns ``True`` when the provider
    confirms the message exists, ``False`` when the provider explicitly did NOT
    find it, and ``None`` when the question cannot be asked. An adapter that does
    not implement this simply cannot probe — it is not an error, and "cannot
    probe" is never evidence about the message. Use :func:`adapter_probe` to
    obtain the optional probe without assuming the hook exists.
    """

    def probe_message(self, provider_message_id: str) -> bool | None: ...


def adapter_probe(adapter: object) -> TransmissionProbe | None:
    """Return ``adapter``'s optional read-back probe, or ``None`` when it cannot probe.

    ``None`` is the default for every adapter that does not implement the
    :class:`ProbeCapable` hook — the honest answer is "this adapter cannot verify
    anything with the provider", never a fabricated confirmation.
    """
    candidate = getattr(adapter, "probe_message", None)
    if not callable(candidate):
        return None
    return cast("TransmissionProbe", candidate)


class TelegramAdapter:
    """Send-only adapter. Gateway remains the sole Telegram update consumer.

    Internal state transitions (``ProgressEvent.event_type == "transition"``)
    are folded into ONE live progress bubble per task: the first transition
    creates the message and every later one EDITS it. A single owner request
    therefore costs one progress message plus one terminal message instead of
    one message per internal state (FP-L07: the live stack produced 9 separate
    status messages — ``RECEIVED — gateway accepted task`` … ``DONE — …``).

    Read-back is deliberately NOT implemented: the Telegram Bot API offers no
    get-message endpoint (there is ``getUpdates``/``getChat``, but no way to ask
    whether a specific ``message_id`` still exists in a chat), so a
    ``probe_message`` implementation here could only fabricate an answer. The
    class therefore has no probe hook and :func:`adapter_probe` reports "cannot
    probe"; Telegram receipts stay at ``SEND_ACK`` and can never be ``REFUTED``
    locally. A real read-back needs a provider with a lookup API.
    """

    name = "telegram"

    #: ``event_type`` value that marks an internal transition (see ProgressEvent).
    PROGRESS_EVENT_TYPE = "transition"

    def __init__(
        self,
        bot_token: str | None = None,
        chat_id: str | None = None,
        base_url: str = "https://api.telegram.org",
        mock: bool = False,
        timeout: int = 10,
    ) -> None:
        self.bot_token = bot_token or ""
        self.chat_id = chat_id or ""
        self.base_url = base_url.rstrip("/")
        self.mock = mock
        self.timeout = timeout
        self.delivered_events: list[tuple[ProgressEvent, str]] = []
        self.seen_keys: set[str] = set()
        #: task_id → Telegram message_id of the single live progress bubble.
        self._progress_message_ids: dict[str, int] = {}
        #: task_id → text currently shown in that bubble (skips no-op edits).
        self._progress_texts: dict[str, str] = {}


    def send_file(self, path: str, caption: str = "") -> dict[str, Any]:
        """Send a document/file to the configured chat via sendDocument (multipart)."""
        if self.mock:
            return {"ok": True, "mock": True, "path": path}
        import os
        missing = [
            name
            for name, value in (("bot_token", self.bot_token), ("chat_id", self.chat_id))
            if not value
        ]
        if missing:
            raise DeliveryConfigError(self.name, missing)
        if not os.path.isfile(path):
            raise FileNotFoundError(path)
        filename = os.path.basename(path)
        boundary = "----AntigonaBoundary" + os.urandom(8).hex()
        parts: list[bytes] = []
        parts = []
        parts.append(("--" + boundary + "\r\n").encode())
        parts.append(('Content-Disposition: form-data; name="chat_id"\r\n\r\n' + str(self.chat_id) + "\r\n").encode())
        parts.append(("--" + boundary + "\r\n").encode())
        parts.append(
            ('Content-Disposition: form-data; name="document"; filename="' + filename + '"\r\n').encode()
            + b"Content-Type: application/octet-stream\r\n\r\n"
        )
        with open(path, "rb") as fh:
            parts.append(fh.read())
        parts.append(b"\r\n")
        if caption:
            parts.append(("--" + boundary + "\r\n").encode())
            parts.append(('Content-Disposition: form-data; name="caption"\r\n\r\n' + caption + "\r\n").encode())
        parts.append(("--" + boundary + "--\r\n").encode())
        body = b"".join(parts)
        request = urllib.request.Request(
            self.base_url + "/bot" + self.bot_token + "/sendDocument",
            body,
            {"Content-Type": "multipart/form-data; boundary=" + boundary},
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            raw = response.read()
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            parsed = {}
        if not isinstance(parsed, dict) or not parsed.get("ok"):
            raise DeliveryProviderRejected(self.name)
        return {"ok": True, "path": path, "file_name": filename}

    def send_voice(self, path: str, caption: str = "") -> dict[str, Any]:
        """Send an Ogg Opus file to the configured chat as a voice message (sendVoice)."""
        if self.mock:
            return {"ok": True, "mock": True, "path": path}
        import os as _os
        missing = [
            name
            for name, value in (("bot_token", self.bot_token), ("chat_id", self.chat_id))
            if not value
        ]
        if missing:
            raise DeliveryConfigError(self.name, missing)
        if not _os.path.isfile(path):
            raise FileNotFoundError(path)
        filename = _os.path.basename(path)
        boundary = "----AntigonaBoundary" + _os.urandom(8).hex()
        parts: list[bytes] = []
        parts.append(("--" + boundary + "\r\n").encode())
        parts.append(('Content-Disposition: form-data; name="chat_id"\r\n\r\n' + str(self.chat_id) + "\r\n").encode())
        parts.append(("--" + boundary + "\r\n").encode())
        parts.append(
            ('Content-Disposition: form-data; name="voice"; filename="' + filename + '"\r\n').encode()
            + b"Content-Type: audio/ogg\r\n\r\n"
        )
        with open(path, "rb") as fh:
            parts.append(fh.read())
        parts.append(b"\r\n")
        if caption:
            parts.append(("--" + boundary + "\r\n").encode())
            parts.append(('Content-Disposition: form-data; name="caption"\r\n\r\n' + caption + "\r\n").encode())
        parts.append(("--" + boundary + "--\r\n").encode())
        body = b"".join(parts)
        request = urllib.request.Request(
            self.base_url + "/bot" + self.bot_token + "/sendVoice",
            body,
            {"Content-Type": "multipart/form-data; boundary=" + boundary},
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            raw = response.read()
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            parsed = {}
        if not isinstance(parsed, dict) or not parsed.get("ok"):
            raise DeliveryProviderRejected(self.name)
        return {"ok": True, "path": path, "file_name": filename}

    def deliver(self, event: ProgressEvent, idempotency_key: str) -> bool:
        """Historical boolean contract — see :meth:`deliver_outcome`."""
        return self.deliver_outcome(event, idempotency_key).transmitted

    def deliver_outcome(self, event: ProgressEvent, idempotency_key: str) -> DeliveryOutcome:
        """Send ``event`` and report transmission plus provider read-back.

        Returns a :class:`~antigona.delivery.readback.DeliveryOutcome`. When
        Telegram answers ``ok`` with a ``result.message_id`` the identifier is
        preserved and the level is ``SEND_ACK`` — a *transmission* acknowledgement
        only, never a claim that a human read the message. When the provider
        returns no identifier the level stays ``UNSUPPORTED``. The mock branch
        never contacts anyone, so it is ``transmitted=False`` / ``UNSUPPORTED``.

        An INTERNAL transition (``event.event_type == "transition"``) is folded
        into the task's single live progress bubble: created on the first
        transition and edited afterwards, so the owner sees a moving indicator
        rather than one message per internal state (FP-L07).
        """
        if self.mock:
            if idempotency_key not in self.seen_keys:
                self.delivered_events.append((event, idempotency_key))
                self.seen_keys.add(idempotency_key)
            return DeliveryOutcome(transmitted=False, read_back_status=READ_BACK_UNSUPPORTED)

        missing = [
            name
            for name, value in (("bot_token", self.bot_token), ("chat_id", self.chat_id))
            if not value
        ]
        if missing:
            raise DeliveryConfigError(self.name, missing)

        # ``event.message`` crosses the durable public boundary HTML-escaped.
        # Telegram is called without parse_mode, so restore the original plain
        # text once, then enforce Telegram's UTF-16-unit limit.
        text = html.unescape(f"{event.task_id}: {event.status} — {event.message}")
        text = _truncate_telegram_text(text)

        if event.event_type == self.PROGRESS_EVENT_TYPE:
            return self._deliver_progress(event, text, idempotency_key)
        # A terminal/owner-facing message closes the progress lifecycle: the
        # next request of this task starts a fresh indicator.
        self._progress_message_ids.pop(event.task_id, None)
        self._progress_texts.pop(event.task_id, None)
        return self._send_message(text, idempotency_key)

    # ── Single live progress indicator ───────────────────────────────────

    def _deliver_progress(
        self,
        event: ProgressEvent,
        text: str,
        idempotency_key: str,
    ) -> DeliveryOutcome:
        """Create the task's progress bubble once, then EDIT it in place."""
        message_id = self._progress_message_ids.get(event.task_id)

        if message_id is not None and self._progress_texts.get(event.task_id) == text:
            # A retry/duplicate outbox row for a state the bubble already shows:
            # there is nothing to transmit, and editing would only produce the
            # provider's "message is not modified" error.
            return DeliveryOutcome(
                transmitted=True,
                provider_message_id=str(message_id),
                read_back_status=READ_BACK_SEND_ACK,
            )

        if message_id is None:
            outcome = self._send_message(text, idempotency_key)
            if outcome.provider_message_id is not None:
                self._progress_message_ids[event.task_id] = int(outcome.provider_message_id)
                self._progress_texts[event.task_id] = text
            return outcome

        try:
            outcome = self._edit_message(message_id, text, idempotency_key)
        except DeliveryProviderRejected:
            # The bubble is gone (deleted) or not editable: start a fresh
            # indicator instead of silently dropping the progress.
            self._progress_message_ids.pop(event.task_id, None)
            self._progress_texts.pop(event.task_id, None)
            return self._deliver_progress(event, text, idempotency_key)
        self._progress_texts[event.task_id] = text
        return outcome

    def _post(
        self,
        method: str,
        payload: dict[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        body = json.dumps(payload).encode()
        request = urllib.request.Request(
            f"{self.base_url}/bot{self.bot_token}/{method}",
            body,
            {"Content-Type": "application/json", "Idempotency-Key": idempotency_key},
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            raw_body = response.read()
        try:
            parsed = json.loads(raw_body)
        except (TypeError, ValueError):
            parsed = {}
        return parsed if isinstance(parsed, dict) else {}

    def _send_message(self, text: str, idempotency_key: str) -> DeliveryOutcome:
        """``sendMessage`` — one NEW message in the chat."""
        parsed = self._post(
            "sendMessage",
            {"chat_id": self.chat_id, "text": text},
            idempotency_key,
        )
        if not parsed.get("ok"):
            raise DeliveryProviderRejected(self.name)
        result = parsed.get("result")
        message_id = result.get("message_id") if isinstance(result, dict) else None
        if message_id is None:
            return DeliveryOutcome(transmitted=True, read_back_status=READ_BACK_UNSUPPORTED)
        return DeliveryOutcome(
            transmitted=True,
            provider_message_id=str(message_id),
            read_back_status=READ_BACK_SEND_ACK,
        )

    def _edit_message(
        self,
        message_id: int,
        text: str,
        idempotency_key: str,
    ) -> DeliveryOutcome:
        """``editMessageText`` — update the existing progress bubble in place."""
        parsed = self._post(
            "editMessageText",
            {"chat_id": self.chat_id, "message_id": message_id, "text": text},
            idempotency_key,
        )
        if not parsed.get("ok"):
            description = str(parsed.get("description", "")).lower()
            if "not modified" in description:
                return DeliveryOutcome(
                    transmitted=True,
                    provider_message_id=str(message_id),
                    read_back_status=READ_BACK_SEND_ACK,
                )
            raise DeliveryProviderRejected(self.name)
        return DeliveryOutcome(
            transmitted=True,
            provider_message_id=str(message_id),
            read_back_status=READ_BACK_SEND_ACK,
        )


class FakeAdapter:
    """In-process delivery simulator (the ``fake`` channel). Never contacts an
    external service, so ``deliver`` always reports a simulated dispatch.
    """

    name = "fake"

    def __init__(self, fail_times: int = 0) -> None:
        self.events: list[tuple[ProgressEvent, str]] = []
        self.fail_times = fail_times
        self.seen: set[str] = set()

    def deliver(self, event: ProgressEvent, idempotency_key: str) -> bool:
        if self.fail_times:
            self.fail_times -= 1
            raise RuntimeError("injected delivery failure")
        if idempotency_key not in self.seen:
            self.events.append((event, idempotency_key))
            self.seen.add(idempotency_key)
        return False
