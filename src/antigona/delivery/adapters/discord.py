from __future__ import annotations

from typing import TYPE_CHECKING

from antigona.delivery.errors import DeliveryConfigError

if TYPE_CHECKING:
    from antigona.delivery.adapter import ProgressEvent


class DiscordAdapter:
    """Discord delivery adapter with mock runtime and lazy HTTP/SDK dispatch."""

    name = "discord"

    def __init__(
        self,
        webhook_url: str | None = None,
        token: str | None = None,
        channel_id: str | None = None,
        mock: bool = True,
        fail_times: int = 0,
        timeout: int = 10,
    ) -> None:
        self.webhook_url = webhook_url
        self.token = token
        self.channel_id = channel_id
        self.mock = mock
        self.fail_times = fail_times
        self.timeout = timeout
        self.delivered_events: list[tuple[ProgressEvent, str]] = []
        self.seen_keys: set[str] = set()

    def deliver(self, event: ProgressEvent, idempotency_key: str) -> bool:
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("discord delivery failure: service unavailable")

        if self.mock:
            if idempotency_key not in self.seen_keys:
                self.delivered_events.append((event, idempotency_key))
                self.seen_keys.add(idempotency_key)
            return False

        if not (self.webhook_url or (self.token and self.channel_id)):
            raise DeliveryConfigError(self.name, ["webhook_url_or_token_and_channel_id"])

        # Lazy HTTP import for real delivery
        import json
        import urllib.request

        payload = {"content": f"[{event.task_id}] {event.status}: {event.message}"}
        body = json.dumps(payload).encode("utf-8")

        if self.webhook_url:
            req = urllib.request.Request(
                self.webhook_url,
                data=body,
                headers={"Content-Type": "application/json", "X-Idempotency-Key": idempotency_key},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                if resp.status not in (200, 204):
                    raise RuntimeError(f"discord webhook failed with status {resp.status}")
        elif self.token and self.channel_id:
            url = f"https://discord.com/api/v10/channels/{self.channel_id}/messages"
            req = urllib.request.Request(
                url,
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bot {self.token}",
                    "X-Idempotency-Key": idempotency_key,
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                if resp.status not in (200, 201):
                    raise RuntimeError(f"discord bot message failed with status {resp.status}")
        return True
