from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .contracts import ArtifactResult, Evidence, ToolResult, WriteFileInput
from .ownership.epoch import FenceDeniedError, OwnershipContext
from .ownership.wiring import enforce_write_fence
from .path_boundary import (
    PathIdentityError,
    has_unsafe_relative_path_syntax,
    is_contained_by_root_identity,
)
from .sandbox.runner import (
    DEFAULT_RUNTIME,
    SandboxProfile,
    build_run_argv,
    resolve_workspace_uid_gid,
)

# ── Undo trail ──────────────────────────────────────────────────────────

class UndoEntry:
    """Record of a file operation that can be rolled back."""
    def __init__(self, op: str, path: str, backup: str | None, timestamp: float) -> None:
        self.op = op
        self.path = path
        self.backup = backup
        self.timestamp = timestamp

    def to_dict(self) -> dict[str, Any]:
        return {"op": self.op, "path": self.path, "backup": self.backup, "timestamp": self.timestamp}


_undo_stack: list[UndoEntry] = []


def record_undo(op: str, path: str, backup: str | None) -> None:
    """Record a file operation for potential rollback."""
    _undo_stack.append(UndoEntry(op, path, backup, __import__('time').time()))


def undo_last() -> bool:
    """Roll back the last recorded operation. Returns True on success."""
    if not _undo_stack:
        return False
    entry = _undo_stack.pop()
    if entry.backup and Path(entry.backup).exists():
        try:
            shutil.copy2(entry.backup, entry.path)
            return True
        except OSError:
            return False
    return False


def get_undo_log() -> list[dict[str, Any]]:
    """Return the full undo trail as a list of dicts."""
    return [e.to_dict() for e in _undo_stack]


def clear_undo_log() -> None:
    """Clear the undo trail."""
    _undo_stack.clear()


class WorkspaceViolation(ValueError): pass
class SandboxUnavailable(RuntimeError): pass


def validate_relative_path(workspace: Path, relative_path: str) -> None:
    if has_unsafe_relative_path_syntax(relative_path):
        raise WorkspaceViolation("unsafe workspace-relative path")
    raw=Path(relative_path)
    if raw.is_absolute() or not raw.parts or any(part in {"", ".", ".."} for part in raw.parts): raise WorkspaceViolation("unsafe workspace-relative path")
    root=workspace.resolve()
    candidate=(root/raw).resolve(strict=False)
    try: candidate.relative_to(root)
    except ValueError as exc: raise WorkspaceViolation("path escapes workspace") from exc
    current=root
    for part in raw.parts:
        current=current/part
        if current.is_symlink(): raise WorkspaceViolation("symlink path component forbidden")
    # P1-FENCE / OS-identity layer: the checks above compare strings.  This one
    # asks the kernel whether the candidate's deepest existing ancestor really
    # is the workspace root (same st_dev + st_ino), and denies when the root
    # itself has no OS identity — a root that does not exist must never be
    # treated as an unrestricted boundary.  The layer is independent of the
    # resolve() above: it also refuses an unresolved path with a symlink
    # component and a mount point inside the root that reaches another device.
    try:
        contained = is_contained_by_root_identity(root, candidate)
    except PathIdentityError as exc:
        raise WorkspaceViolation(f"workspace root has no OS identity: {exc}") from exc
    if not contained:
        raise WorkspaceViolation("path escapes workspace (identity mismatch)")


class SandboxBackend:
    #: Live ownership fencing token (DF-WO2-003-full); None when disabled/unbound.
    ownership: OwnershipContext | None = None

    def write(self, path: str, content: str, timeout: int) -> ToolResult: raise NotImplementedError
    def read_and_hash(self, path: str) -> tuple[str, str]: raise NotImplementedError

    def bind_ownership(self, ownership: OwnershipContext | None) -> None:
        """Bind a live fencing token to this backend (DF-WO2-003-full)."""
        self.ownership = ownership

    def _fence_write(self) -> ToolResult | None:
        """Enforce the ownership fence at the mutation boundary.

        Returns a failed ``ToolResult`` when a protected write is DENIED
        (fail-closed, INV-06), else ``None`` so the write proceeds.  When
        ownership is disabled this is a no-op (backward-compat).
        """
        try:
            enforce_write_fence(self.ownership, "workspace.write_text")
        except FenceDeniedError as exc:
            return ToolResult(False, "failed", error=f"protected write denied: {exc.check.reason}")
        return None


class InProcessTestBackend(SandboxBackend):
    """Hermetic test-only backend using openat/O_NOFOLLOW containment."""
    def __init__(self, workspace: Path, *, test_mode: bool, ownership: OwnershipContext | None = None) -> None:
        if not test_mode: raise RuntimeError("in-process backend is test-only")
        self.workspace=workspace.resolve(); self.workspace.mkdir(parents=True, exist_ok=True)
        self.ownership = ownership

    def _parent_fd(self, path: str) -> tuple[int, str]:
        validate_relative_path(self.workspace, path); parts=Path(path).parts
        if os.name == "nt":
            # Windows: no O_DIRECTORY / dir_fd. Fall back to mkdir parents + plain open.
            parent_dir = self.workspace / Path(*parts[:-1]) if parts[:-1] else self.workspace
            parent_dir.mkdir(parents=True, exist_ok=True)
            return -1, parts[-1]
        fd=os.open(self.workspace, os.O_RDONLY|os.O_DIRECTORY)
        try:
            for part in parts[:-1]:
                try: os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError: pass
                new=os.open(part, os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW, dir_fd=fd); os.close(fd); fd=new
            return fd, parts[-1]
        except Exception: os.close(fd); raise

    def write(self, path: str, content: str, timeout: int) -> ToolResult:
        del timeout
        fenced = self._fence_write()
        if fenced is not None:
            return fenced
        # Bound before the try so the rollback handler below never reads an
        # unassigned local if _parent_fd() raises (e.g. a symlink escape).
        backup_path: str | None = None
        full_path: Path | None = None
        try:
            parent,name=self._parent_fd(path)
            # Backup existing file for undo-trail
            full_path = (self.workspace / path).resolve()
            if full_path.exists():
                backup_path = str(full_path) + ".bak"
                shutil.copy2(full_path, backup_path)
            try:
                if os.name == "nt":
                    # Windows: no dir_fd / O_NOFOLLOW. Write through the resolved path.
                    # BUG ANT-007 (wave3, class B): write_text() translates \n to
                    # \r\n (text mode) — write raw bytes for byte-identical output.
                    full_path.parent.mkdir(parents=True, exist_ok=True)
                    full_path.write_bytes(content.encode("utf-8"))
                else:
                    fd=os.open(name, os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_NOFOLLOW, 0o600, dir_fd=parent)
                    with os.fdopen(fd,"w",encoding="utf-8") as stream: stream.write(content)
            finally:
                if os.name != "nt":
                    os.close(parent)
            observed,digest=self.read_and_hash(path); size=len(observed.encode())
            from .filesystem import record_undo
            record_undo("write", str(full_path), backup_path)
            return ToolResult(True,"completed",{"content":observed},evidence=[Evidence("read_back",observed),Evidence("sha256",digest)],artifacts=[ArtifactResult(path,digest,size)])
        except (OSError,WorkspaceViolation) as exc:
            # Rollback on error if backup exists
            if backup_path and full_path and Path(backup_path).exists():
                try: shutil.copy2(backup_path, full_path)
                except OSError: pass
            return ToolResult(False,"failed",error=str(exc))

    def read_and_hash(self,path: str)->tuple[str,str]:
        if os.name == "nt":
            full = (self.workspace / path).resolve()
            data = full.read_bytes()
            return data.decode(), hashlib.sha256(data).hexdigest()
        parent,name=self._parent_fd(path)
        try:
            fd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW,dir_fd=parent)
            with os.fdopen(fd,"rb") as stream: data=stream.read()
        finally: os.close(parent)
        return data.decode(),hashlib.sha256(data).hexdigest()


def _extract_last_error_line(stderr: str) -> str:
    lines = [line.strip() for line in stderr.strip().splitlines() if line.strip()]
    if not lines:
        return "container exited non-zero"
    for line in reversed(lines):
        if any(err_type in line for err_type in ("Error:", "Exception:", "Permission denied", "No space", "File exists", "Is a directory", "Not a directory")):
            return line
    return lines[-1]


class DockerSandboxBackend(SandboxBackend):
    def __init__(self, workspace: Path, image: str="python:3.12-alpine", runtime: str=DEFAULT_RUNTIME, ownership: OwnershipContext | None = None) -> None:
        self.workspace=workspace.resolve(); self.workspace.mkdir(parents=True,exist_ok=True,mode=0o750); self.workspace.chmod(0o750); self.image=image; self.runtime=runtime; self.ownership = ownership
        uid, gid = resolve_workspace_uid_gid(self.workspace)
        self._profile=SandboxProfile(workspace=self.workspace,image=image,runtime=runtime,
                                     uid=uid,
                                     gid=gid)

    def _docker(self,args:list[str],timeout:int)->subprocess.CompletedProcess[str]:
        command=build_run_argv(self._profile,args)
        try: return subprocess.run(command,capture_output=True,text=True,timeout=timeout,check=False)
        except (FileNotFoundError,subprocess.TimeoutExpired) as exc: raise SandboxUnavailable(str(exc)) from exc

    def write(self,path:str,content:str,timeout:int)->ToolResult:
        fenced = self._fence_write()
        if fenced is not None:
            return fenced
        try:
            validate_relative_path(self.workspace,path)
            script="import os,pathlib,sys\np=pathlib.Path(sys.argv[1]); p.parent.mkdir(parents=True,exist_ok=True)\nfd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_NOFOLLOW,0o600); os.write(fd,sys.stdin.buffer.read()); os.close(fd)"
            command=build_run_argv(self._profile,["python","-c",script,path],interactive=True)
            proc=subprocess.run(command, input=content.encode("utf-8"), capture_output=True, timeout=timeout, check=False)
            if proc.returncode:
                _raw_err = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
                _err = _extract_last_error_line(_raw_err)
                return ToolResult(False, "failed", error=f"sandbox blocked: {_err}")
            observed,digest=self.read_and_hash(path)
            return ToolResult(True,"completed",{"content":observed},evidence=[Evidence("read_back",observed),Evidence("sha256",digest)],artifacts=[ArtifactResult(path,digest,len(observed.encode()))])
        except subprocess.TimeoutExpired:
            return ToolResult(False,"failed",error="tool timeout")
        except (FileNotFoundError,SandboxUnavailable,WorkspaceViolation) as exc: return ToolResult(False,"failed",error=f"sandbox unavailable/unsafe: {exc}")

    def read_and_hash(self,path:str)->tuple[str,str]:
        validate_relative_path(self.workspace,path); data=(self.workspace/path).read_bytes(); return data.decode(),hashlib.sha256(data).hexdigest()


class WorkspaceFileTool:
    """Sandboxed file-write tool (the live ``workspace.write_text`` surface).

    DF-WO2-003-full: carries an optional live ownership fencing token
    (``ownership``) and pushes it onto the backend so every actual write is
    fenced at the mutation boundary.  When ownership is disabled this is inert
    (backward-compat); when enabled, a stale/unauthorised owner's write is
    DENIED before any filesystem mutation (INV-04/INV-06).
    """
    name="workspace.write_text"; description="Write UTF-8 text in sandbox"; risk_level="medium"; timeout_seconds=10; requires_approval=True; sandbox_required=True
    def __init__(self,backend:SandboxBackend,timeout_seconds:int=10,ownership:OwnershipContext | None=None)->None:
        self.backend=backend; self.timeout_seconds=timeout_seconds; self.ownership=ownership
    def bind_ownership(self, ownership: OwnershipContext | None) -> None:
        """Bind a live fencing token to this tool (DF-WO2-003-full)."""
        self.ownership = ownership
        self.backend.ownership = ownership
    def execute(self,arguments:WriteFileInput)->ToolResult:
        self.backend.ownership = self.ownership
        return self.backend.write(arguments.path,arguments.content,self.timeout_seconds)
    def read_and_hash(self,path:str)->tuple[str,str]: return self.backend.read_and_hash(path)
