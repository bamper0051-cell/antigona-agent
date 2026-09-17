from __future__ import annotations

import smtplib
import socket
import urllib.error
from collections.abc import Sequence


class DeliveryError(Exception):
    """Base class for delivery-layer errors. ``code`` is the only detail ever persisted."""

    code = "delivery.error"


class DeliveryPermanentError(DeliveryError):
    """Non-retryable: the worker must mark the outbox row FAILED immediately."""

    code = "delivery.permanent_error"


class DeliveryConfigError(DeliveryPermanentError):
    """Adapter is missing required real-transport configuration.

    Carries field names only, never values, so ``code`` is always safe to persist.
    """

    def __init__(self, adapter: str, missing_fields: Sequence[str]) -> None:
        self.adapter = adapter
        self.missing_fields = list(missing_fields)
        self.code = f"delivery.configuration_error:{adapter}:{','.join(self.missing_fields)}"
        super().__init__(self.code)


class UnknownChannelError(DeliveryPermanentError, ValueError):
    """Unknown or disabled delivery channel requested."""

    code = "delivery.unknown_channel"


class DeliveryProviderRejected(DeliveryError):
    """Provider responded with HTTP success but rejected the message at the body level."""

    def __init__(self, channel: str) -> None:
        self.code = f"delivery.provider_rejected:{channel}"
        super().__init__(self.code)


def sanitize_delivery_error(exc: Exception) -> str:
    """Map a delivery exception to a bounded, secret-free, fixed-vocabulary code.

    Raw transport exceptions can embed webhook URLs, bot tokens, or SMTP auth
    text, and there is no way to enumerate every SDK's leaky repr, so the
    original exception message is never read here -- only the pre-vetted
    ``DeliveryError.code`` (adapter + field names, never values) and stdlib
    network/SMTP error *types* inform the result. Mirrors the fixed-category
    style of ``antigona.worker._worker_failure_code``.
    """
    if isinstance(exc, DeliveryError):
        return exc.code[:200]
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return "delivery.timeout"
    if isinstance(exc, (urllib.error.HTTPError, urllib.error.URLError)):
        return "delivery.transport_error"
    if isinstance(exc, smtplib.SMTPException):
        return "delivery.smtp_error"
    if isinstance(exc, OSError):
        return "delivery.transport_error"
    return "delivery.execution_error"
