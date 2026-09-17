from __future__ import annotations

from typing import TYPE_CHECKING

from antigona.delivery.errors import DeliveryConfigError

if TYPE_CHECKING:
    from antigona.delivery.adapter import ProgressEvent


class EmailAdapter:
    """SMTP email delivery adapter with mock runtime and lazy smtplib dispatch."""

    name = "email"

    def __init__(
        self,
        smtp_host: str | None = None,
        smtp_port: int = 587,
        user: str | None = None,
        password: str | None = None,
        sender: str | None = None,
        recipient: str | None = None,
        use_tls: bool = True,
        mock: bool = True,
        fail_times: int = 0,
        timeout: int = 10,
    ) -> None:
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.user = user
        self.password = password
        self.sender = sender
        self.recipient = recipient
        self.use_tls = use_tls
        self.mock = mock
        self.fail_times = fail_times
        self.timeout = timeout
        self.delivered_events: list[tuple[ProgressEvent, str]] = []
        self.seen_keys: set[str] = set()

    def deliver(self, event: ProgressEvent, idempotency_key: str) -> bool:
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("email delivery failure: SMTP connection timed out")

        if self.mock:
            if idempotency_key not in self.seen_keys:
                self.delivered_events.append((event, idempotency_key))
                self.seen_keys.add(idempotency_key)
            return False

        missing = [
            name
            for name, value in (
                ("smtp_host", self.smtp_host),
                ("sender", self.sender),
                ("recipient", self.recipient),
            )
            if not value
        ]
        # AUTH is optional only when both user and password are absent; a
        # half-configured pair means the operator intended AUTH but botched it.
        if bool(self.user) != bool(self.password):
            missing.append("smtp_auth_pair")
        if missing:
            raise DeliveryConfigError(self.name, missing)
        assert self.smtp_host and self.sender and self.recipient

        import smtplib
        from email.mime.text import MIMEText

        msg = MIMEText(
            f"Task ID: {event.task_id}\nStatus: {event.status}\nMessage: {event.message}"
        )
        msg["Subject"] = f"[Antigona] Task {event.task_id} - {event.status}"
        msg["From"] = self.sender
        msg["To"] = self.recipient
        msg["X-Idempotency-Key"] = idempotency_key

        with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=self.timeout) as client:
            if self.use_tls:
                client.starttls()
            if self.user and self.password:
                client.login(self.user, self.password)
            client.send_message(msg)
        return True
