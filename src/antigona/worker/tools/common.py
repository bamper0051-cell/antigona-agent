from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from antigona.path_boundary import (
    PathIdentityError,
    has_unsafe_relative_path_syntax,
    is_contained_by_root_identity,
)


class ToolError(ValueError):
    pass


@dataclass(frozen=True)
class WorkspaceGuard:
    workspace: Path

    def __post_init__(self) -> None:
        root = self.workspace.resolve()
        root.mkdir(parents=True, exist_ok=True)
        root.chmod(0o750)
        object.__setattr__(self, "workspace", root)

    def resolve(self, relative_path: str) -> Path:
        if has_unsafe_relative_path_syntax(relative_path):
            raise ToolError("path must be workspace-relative: outside boundary (escapes workspace)")
        raw = Path(relative_path)
        if raw.is_absolute() or not raw.parts:
            raise ToolError("path must be workspace-relative: outside boundary (escapes workspace)")
        if any(part in {".", "..", ""} for part in raw.parts):
            raise ToolError("path traversal is forbidden: outside boundary (escapes workspace)")
        candidate = (self.workspace / raw).resolve(strict=False)
        try:
            candidate.relative_to(self.workspace)
        except ValueError as exc:
            raise ToolError("path escapes workspace: outside boundary") from exc
        current = self.workspace
        for part in raw.parts:
            current = current / part
            if current.is_symlink():
                raise ToolError("symlink path component is forbidden")
        # P1-FENCE / OS-identity layer: the lexical check above compares
        # strings; this one asks the kernel.  A path whose deepest existing
        # ancestor is not the same directory (same st_dev + st_ino) as the
        # workspace root is refused even when the strings say otherwise, and a
        # root with no OS identity denies (fail closed) instead of allowing.
        # The layer is independent of the resolve() above: it also refuses an
        # unresolved path with a symlink component and a mount point inside the
        # root that reaches another device.
        try:
            contained = is_contained_by_root_identity(self.workspace, candidate)
        except PathIdentityError as exc:
            raise ToolError(f"workspace root has no OS identity: {exc}") from exc
        if not contained:
            raise ToolError("path escapes workspace: outside boundary (identity mismatch)")
        return candidate
