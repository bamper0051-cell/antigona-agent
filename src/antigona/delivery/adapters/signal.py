from __future__ import annotations

from typing import TYPE_CHECKING

from antigona.delivery.errors import DeliveryConfigError

if TYPE_CHECKING:
    from antigona.delivery.adapter import ProgressEvent


class SignalAdapter:
    """Signal REST daemon delivery adapter with mock runtime and lazy HTTP dispatch."""

    name = "signal"

    def __init__(
        self,
        url: str | None = None,
        sender: str | None = None,
        recipient: str | None = None,
        mock: bool = True,
        fail_times: int = 0,
        timeout: int = 10,
    ) -> None:
        self.url = url.rstrip("/") if url else None
        self.sender = sender
        self.recipient = recipient
        self.mock = mock
        self.fail_times = fail_times
        self.timeout = timeout
        self.delivered_events: list[tuple[ProgressEvent, str]] = []
        self.seen_keys: set[str] = set()

    def deliver(self, event: ProgressEvent, idempotency_key: str) -> bool:
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("signal delivery failure: daemon unavailable")

        if self.mock:
            if idempotency_key not in self.seen_keys:
                self.delivered_events.append((event, idempotency_key))
                self.seen_keys.add(idempotency_key)
            return False

        missing = [
            name
            for name, value in (
                ("url", self.url),
                ("sender", self.sender),
                ("recipient", self.recipient),
            )
            if not value
        ]
        if missing:
            raise DeliveryConfigError(self.name, missing)

        import json
        import urllib.request

        target_url = f"{self.url}/v2/send"
        payload = {
            "number": self.sender,
            "recipients": [self.recipient],
            "message": f"[{event.task_id}] {event.status}: {event.message}",
        }
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            target_url,
            data=body,
            headers={"Content-Type": "application/json", "X-Idempotency-Key": idempotency_key},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            if resp.status not in (200, 201, 204):
                raise RuntimeError(f"signal-cli daemon returned HTTP {resp.status}")
        return True
