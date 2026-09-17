from __future__ import annotations

import hashlib
from dataclasses import dataclass

from antigona.ownership.epoch import OwnershipContext
from antigona.ownership.wiring import enforce_write_fence

from .common import WorkspaceGuard


@dataclass(frozen=True)
class FileToolResult:
    path: str
    content: str
    sha256: str
    untrusted: bool = False


class WorkspaceFileTools:
    def __init__(self, guard: WorkspaceGuard, ownership: OwnershipContext | None = None) -> None:
        self.guard = guard
        self.ownership = ownership

    def bind_ownership(self, ownership: OwnershipContext | None) -> None:
        self.ownership = ownership

    def write_text(self, path: str, content: str) -> FileToolResult:
        enforce_write_fence(self.ownership, "workspace.write_text")
        target = self.guard.resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return self.read_text(path)

    def read_text(self, path: str, untrusted: bool = False) -> FileToolResult:
        target = self.guard.resolve(path)
        payload = target.read_text(encoding="utf-8")
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        return FileToolResult(path=path, content=payload, sha256=digest, untrusted=untrusted)
