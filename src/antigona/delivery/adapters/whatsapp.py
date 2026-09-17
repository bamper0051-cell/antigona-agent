from __future__ import annotations

from typing import TYPE_CHECKING

from antigona.delivery.errors import DeliveryConfigError

if TYPE_CHECKING:
    from antigona.delivery.adapter import ProgressEvent


class WhatsAppAdapter:
    """WhatsApp Cloud API delivery adapter with mock runtime and lazy HTTP dispatch."""

    name = "whatsapp"

    def __init__(
        self,
        token: str | None = None,
        phone_id: str | None = None,
        to: str | None = None,
        mock: bool = True,
        fail_times: int = 0,
        timeout: int = 10,
    ) -> None:
        self.token = token
        self.phone_id = phone_id
        self.to = to
        self.mock = mock
        self.fail_times = fail_times
        self.timeout = timeout
        self.delivered_events: list[tuple[ProgressEvent, str]] = []
        self.seen_keys: set[str] = set()

    def deliver(self, event: ProgressEvent, idempotency_key: str) -> bool:
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("whatsapp delivery failure: gateway timeout")

        if self.mock:
            if idempotency_key not in self.seen_keys:
                self.delivered_events.append((event, idempotency_key))
                self.seen_keys.add(idempotency_key)
            return False

        missing = [
            name
            for name, value in (("token", self.token), ("phone_id", self.phone_id), ("to", self.to))
            if not value
        ]
        if missing:
            raise DeliveryConfigError(self.name, missing)

        import json
        import urllib.request

        url = f"https://graph.facebook.com/v18.0/{self.phone_id}/messages"
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": self.to,
            "type": "text",
            "text": {
                "preview_url": False,
                "body": f"[{event.task_id}] {event.status}: {event.message}",
            },
        }
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.token}",
                "X-Idempotency-Key": idempotency_key,
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            if resp.status not in (200, 201):
                raise RuntimeError(f"whatsapp API returned HTTP {resp.status}")
        return True
