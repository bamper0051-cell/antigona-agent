"""First-Class Attachment Contract for Antigona.

Provides a unified Attachment representation for all inbound Telegram files,
ensuring security, hash verification, MIME detection, and workspace containment.
"""

from __future__ import annotations

import datetime
import hashlib
import mimetypes
import re
import secrets
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")

SUPPORTED_EXTENSIONS = frozenset({
    ".txt", ".md", ".json", ".yaml", ".yml", ".toml", ".ini", ".conf", ".cfg",
    ".csv", ".tsv", ".py", ".js", ".ts", ".html", ".css", ".xml",
    ".pdf", ".docx", ".xlsx", ".pptx",
    ".zip", ".tar", ".tar.gz", ".tgz",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg",
    ".mp3", ".wav", ".ogg", ".m4a", ".opus",
})


def sanitize_filename(name: str) -> str:
    """Sanitize filename to prevent directory traversal and bad characters."""
    base = Path(name).name
    clean = _SAFE_NAME_RE.sub("_", base).strip("._")
    if not clean:
        clean = "attachment.bin"
    return clean[:200]


def compute_file_sha256(path: Path | str) -> str:
    """Compute SHA-256 hex digest of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


@dataclass(slots=True)
class Attachment:
    """First-class representation of an inbound or outbound file attachment."""

    attachment_id: str
    telegram_file_id: str
    telegram_file_unique_id: str
    original_filename: str
    safe_filename: str
    mime_type: str
    size: int
    sha256: str
    local_path: str
    source_chat_id: int
    source_message_id: int
    operation_id: str = ""
    caption: str = ""
    received_at: datetime.datetime = field(
        default_factory=lambda: datetime.datetime.now(datetime.UTC)
    )
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_downloaded_file(
        cls,
        local_path: Path | str,
        *,
        telegram_file_id: str,
        telegram_file_unique_id: str = "",
        original_filename: str,
        source_chat_id: int,
        source_message_id: int,
        operation_id: str = "",
        caption: str = "",
        mime_type: str = "",
    ) -> Attachment:
        """Construct Attachment from an already-downloaded local file."""
        p = Path(local_path).resolve()
        safe_name = sanitize_filename(original_filename or p.name)
        file_size = p.stat().st_size if p.exists() else 0
        file_hash = compute_file_sha256(p) if p.exists() and file_size > 0 else ""

        resolved_mime = mime_type
        if not resolved_mime:
            guessed, _ = mimetypes.guess_type(str(p))
            resolved_mime = guessed or "application/octet-stream"

        return cls(
            attachment_id=f"att_{uuid.uuid4().hex[:12]}",
            telegram_file_id=telegram_file_id,
            telegram_file_unique_id=telegram_file_unique_id or f"uniq_{secrets.token_hex(6)}",
            original_filename=original_filename or p.name,
            safe_filename=safe_name,
            mime_type=resolved_mime,
            size=file_size,
            sha256=file_hash,
            local_path=str(p),
            source_chat_id=source_chat_id,
            source_message_id=source_message_id,
            operation_id=operation_id,
            caption=caption,
        )

    def is_supported(self) -> bool:
        """Check if file extension is supported."""
        ext = Path(self.original_filename).suffix.lower()
        return ext in SUPPORTED_EXTENSIONS

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "attachment_id": self.attachment_id,
            "telegram_file_id": self.telegram_file_id,
            "telegram_file_unique_id": self.telegram_file_unique_id,
            "original_filename": self.original_filename,
            "safe_filename": self.safe_filename,
            "mime_type": self.mime_type,
            "size": self.size,
            "sha256": self.sha256,
            "local_path": self.local_path,
            "source_chat_id": self.source_chat_id,
            "source_message_id": self.source_message_id,
            "operation_id": self.operation_id,
            "caption": self.caption,
            "received_at": self.received_at.isoformat(),
        }
