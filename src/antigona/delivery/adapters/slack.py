from __future__ import annotations

from typing import TYPE_CHECKING

from antigona.delivery.errors import DeliveryConfigError, DeliveryProviderRejected

if TYPE_CHECKING:
    from antigona.delivery.adapter import ProgressEvent


class SlackAdapter:
    """Slack delivery adapter with mock runtime and lazy HTTP/SDK dispatch."""

    name = "slack"

    def __init__(
        self,
        webhook_url: str | None = None,
        token: str | None = None,
        channel: str | None = None,
        mock: bool = True,
        fail_times: int = 0,
        timeout: int = 10,
    ) -> None:
        self.webhook_url = webhook_url
        self.token = token
        self.channel = channel
        self.mock = mock
        self.fail_times = fail_times
        self.timeout = timeout
        self.delivered_events: list[tuple[ProgressEvent, str]] = []
        self.seen_keys: set[str] = set()

    def deliver(self, event: ProgressEvent, idempotency_key: str) -> bool:
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("slack delivery failure: API unreachable")

        if self.mock:
            if idempotency_key not in self.seen_keys:
                self.delivered_events.append((event, idempotency_key))
                self.seen_keys.add(idempotency_key)
            return False

        if not (self.webhook_url or (self.token and self.channel)):
            raise DeliveryConfigError(self.name, ["webhook_url_or_token_and_channel"])

        import json
        import urllib.request

        payload = {"text": f"[{event.task_id}] {event.status}: {event.message}"}
        if self.channel:
            payload["channel"] = self.channel
        body = json.dumps(payload).encode("utf-8")

        if self.webhook_url:
            req = urllib.request.Request(
                self.webhook_url,
                data=body,
                headers={"Content-Type": "application/json", "X-Idempotency-Key": idempotency_key},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"slack webhook returned HTTP {resp.status}")
        elif self.token:
            req = urllib.request.Request(
                "https://slack.com/api/chat.postMessage",
                data=body,
                headers={
                    "Content-Type": "application/json; charset=utf-8",
                    "Authorization": f"Bearer {self.token}",
                    "X-Idempotency-Key": idempotency_key,
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"slack API returned HTTP {resp.status}")
                raw_body = resp.read()
            try:
                parsed = json.loads(raw_body)
            except (TypeError, ValueError):
                parsed = {}
            if not isinstance(parsed, dict) or not parsed.get("ok"):
                raise DeliveryProviderRejected(self.name)
        return True
