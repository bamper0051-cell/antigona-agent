from __future__ import annotations

import html
import json
import urllib.request
from dataclasses import dataclass
from typing import Any, Protocol

from .errors import DeliveryConfigError, DeliveryProviderRejected

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


class DeliveryAdapter(Protocol):
    name: str

    def deliver(self, event: ProgressEvent, idempotency_key: str) -> bool:
        """Return ``True`` when the event was actually transmitted to the external
        channel, ``False`` when the dispatch was only simulated (mock runtime).
        A raised exception means the delivery failed and may be retried.
        """
        ...


class TelegramAdapter:
    """Send-only adapter. Gateway remains the sole Telegram update consumer."""

    name = "telegram"

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
        if self.mock:
            if idempotency_key not in self.seen_keys:
                self.delivered_events.append((event, idempotency_key))
                self.seen_keys.add(idempotency_key)
            return False

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
        body = json.dumps({"chat_id": self.chat_id, "text": text}).encode()
        request = urllib.request.Request(
            f"{self.base_url}/bot{self.bot_token}/sendMessage",
            body,
            {"Content-Type": "application/json", "Idempotency-Key": idempotency_key},
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            raw_body = response.read()
        try:
            parsed = json.loads(raw_body)
        except (TypeError, ValueError):
            parsed = {}
        if not isinstance(parsed, dict) or not parsed.get("ok"):
            raise DeliveryProviderRejected(self.name)
        return True


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
