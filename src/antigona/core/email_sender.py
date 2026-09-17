"""Email delivery for Antigona (Gmail SMTP).

Owns the Gmail credentials (``~/.antigona/secrets/gmail_creds.txt``,
``EMAIL=`` / ``PASSWORD=``) and the SMTP send path. Used both by the
``send_email`` builtin tool (conversation mode) and by the Orchestrator's
``send_email`` task branch, so there is exactly one sender implementation.
"""

from __future__ import annotations

import os
import smtplib
import ssl
from collections.abc import Iterable
from email.message import EmailMessage
from pathlib import Path

from antigona.core import paths

CREDS_PATH = paths.secrets_dir() / "gmail_creds.txt"

# Delivery recipient is configured via env — same canonical var the unified
# delivery stack reads (Settings.delivery_email_to / config.py). No hardcoded
# personal default: sending without an explicit "to" and without this env
# fails fast with a readable error.
DELIVERY_EMAIL_TO_ENV = "ANTIGONA_DELIVERY_EMAIL_TO"

DEFAULT_SUBJECT = "Antigona delivery"
DEFAULT_BODY = "Сообщение от Antigona."


def default_recipient() -> str:
    """Recipient from ``ANTIGONA_DELIVERY_EMAIL_TO`` ('' when unset)."""
    return os.getenv(DELIVERY_EMAIL_TO_ENV, "").strip()


def resolve_recipient(to: str | None = None) -> str:
    """Resolve explicit ``to`` or configured env recipient; fail fast."""
    recipient = (to or default_recipient()).strip()
    if not recipient:
        raise RuntimeError(
            "no email recipient: set ANTIGONA_DELIVERY_EMAIL_TO or pass an explicit 'to' address"
        )
    return recipient


def _load_creds(creds_path: str | Path | None = None) -> tuple[str, str]:
    path = Path(creds_path) if creds_path else CREDS_PATH
    email = ""
    password = ""
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("EMAIL="):
                    email = line.split("=", 1)[1].strip()
                elif line.startswith("PASSWORD="):
                    password = line.split("=", 1)[1].strip()
    except OSError as exc:
        raise RuntimeError(f"cannot read gmail creds: {exc}") from exc
    if not email or not password:
        raise RuntimeError("gmail creds missing EMAIL/PASSWORD")
    return email, password


def _guess_mime(path: str) -> tuple[str, str]:
    if path.endswith(".mp3"):
        return "audio", "mpeg"
    if path.endswith((".png", ".jpg", ".jpeg")):
        return "image", "jpeg" if path.endswith((".jpg", ".jpeg")) else "png"
    if path.endswith(".mp4"):
        return "video", "mp4"
    return "application", "octet-stream"


def send_email(
    to: str | None = None,
    subject: str = DEFAULT_SUBJECT,
    body: str = DEFAULT_BODY,
    attachments: Iterable[str] = (),
    *,
    creds_path: str | Path | None = None,
) -> str:
    """Send one email via Gmail SMTP; returns a confirmation message.

    ``attachments`` are absolute-or-relative file paths; relative paths are
    resolved against ``ANTIGONA_WORKSPACE`` (default ``./workspace``). Raises
    on failure — callers shape the error.
    """
    email_addr, password = _load_creds(creds_path)
    recipient = resolve_recipient(to)
    workspace = paths.workspace_dir()

    msg = EmailMessage()
    msg["Subject"] = subject or DEFAULT_SUBJECT
    msg["From"] = email_addr
    msg["To"] = recipient
    msg.set_content(body or DEFAULT_BODY)

    attached = 0
    for raw in attachments or ():
        path = Path(raw)
        if not path.is_absolute():
            path = workspace / path
        if not path.is_file():
            continue
        maintype, subtype = _guess_mime(path.name)
        with open(path, "rb") as f:
            msg.add_attachment(f.read(), maintype=maintype, subtype=subtype, filename=path.name)
        attached += 1

    context = ssl.create_default_context()
    with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as server:
        server.starttls(context=context)
        server.login(email_addr, password)
        server.send_message(msg)

    return f"Email sent to {recipient} (attachments: {attached})"
