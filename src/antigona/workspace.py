from __future__ import annotations

import hashlib
import importlib
import os
import shutil
import uuid
from abc import ABC, abstractmethod
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from antigona.config import Settings
from antigona.ownership.epoch import OwnershipContext, WritePermit
from antigona.ownership.wiring import bind_workspace_ownership, enforce_write_fence


class ToolError(ValueError):
    """Base tool error."""


class WorkspaceConnectionError(ToolError):
    """Raised when an execution backend fails to connect or reach remote environment."""


@dataclass(frozen=True)
class WorkspaceFileResult:
    path: str
    content: str
    sha256: str
    untrusted: bool = False


@dataclass(frozen=True)
class WorkspaceShellResult:
    command: tuple[str, ...]
    exit_code: int
    stdout: str
    stderr: str
    untrusted: bool = False


class BaseWorkspace(ABC):
    """Abstract base class for all execution-backend workspaces."""

    @property
    @abstractmethod
    def backend_type(self) -> str:
        """Return the unique string identifier for this backend type."""
        ...

    @property
    @abstractmethod
    def root_path(self) -> Path:
        """Return the root path of the workspace."""
        ...

    @property
    @abstractmethod
    def is_connected(self) -> bool:
        """Return True if the workspace backend is ready and accessible."""
        ...

    def connect(self) -> None:
        """Connect or probe the workspace backend."""
        return None

    @abstractmethod
    def write_file(self, path: str | Path, content: str) -> WorkspaceFileResult:
        """Write text content to a file inside the workspace."""
        ...

    @abstractmethod
    def read_file(self, path: str | Path, untrusted: bool = False) -> WorkspaceFileResult:
        """Read text content from a file inside the workspace."""
        ...

    @abstractmethod
    def execute_command(
        self,
        command: Sequence[str],
        timeout: int = 30,
        *,
        approved: bool = False,
        correlation_id: str = "",
        task_id: str = "",
    ) -> WorkspaceShellResult:
        """Execute a shell command inside the workspace."""
        ...


    def cleanup(self) -> None:
        """Clean up resources associated with the workspace backend."""
        return None

    # ── Ownership fence (DF-WO2-003-full) ────────────────────────────────────
    # The ownership gate used to be LocalWorkspace-only; the live worker writes
    # via WorkspaceFileTool / DockerShellTool and the remote backends, none of
    # which were fenced.  These base helpers give EVERY backend the same fencing
    # contract so a stale/unauthorised owner can never mutate a protected
    # workspace through ANY surface when ownership is enabled.

    _ownership: OwnershipContext | None = None

    def bind_ownership(self, ownership: OwnershipContext | None) -> None:
        """Bind a live fencing token to this workspace (DF-WO2-003-full)."""
        self._ownership = ownership

    @property
    def ownership(self) -> OwnershipContext | None:
        """The live ownership fencing token bound to this workspace.

        ``None`` when ownership is disabled or no token has been bound
        (backward-compat).  ``WorkspaceFactory.create_workspace`` binds this
        via ``bind_workspace_ownership``; the worker forwards the SAME token
        onto the shared ``WorkspaceFileTool`` / ``DockerShellTool`` so every
        live write/execute surface is fenced together (DF-WO2-003-full).
        """
        return self._ownership

    def _acquire_write_permit(self, surface: str | None = None) -> WritePermit | None:
        """Enforce the ownership fence at this backend's mutation boundary."""
        return enforce_write_fence(self._ownership, surface or self.backend_type)

    @staticmethod
    def _release_write_permit(permit: WritePermit | None) -> None:
        # Permits are time-bounded holds; nothing to revoke. Kept explicit so a
        # caller can extend this if a revoke protocol is ever added.
        return None

    @contextmanager
    def _fenced(self, surface: str) -> Iterator[None]:
        """Hold the ownership write permit across a mutation (TOCTOU closure)."""
        permit = self._acquire_write_permit(surface)
        try:
            yield
        finally:
            self._release_write_permit(permit)



class LocalWorkspace(BaseWorkspace):
    """Local filesystem workspace backend using Antigona WorkspaceGuard."""

    def __init__(
        self,
        root_path: Path | str = "./workspace",
        *,
        ownership: OwnershipContext | None = None,
    ) -> None:
        from antigona.worker.tools.common import WorkspaceGuard
        from antigona.worker.tools.file_tools import WorkspaceFileTools
        from antigona.worker.tools.shell_tool import WorkspaceShellTool

        self._root_path = Path(root_path).resolve()
        self._guard = WorkspaceGuard(self._root_path)
        self._file_tools = WorkspaceFileTools(self._guard)
        self._shell_tool = WorkspaceShellTool(self._guard)
        self._ownership = ownership

    @property
    def backend_type(self) -> str:
        return "local"

    @property
    def root_path(self) -> Path:
        return self._root_path

    @property
    def is_connected(self) -> bool:
        return True

    def connect(self) -> None:
        return None

    def bind_ownership(self, ownership: OwnershipContext | None) -> None:
        """Bind a live fencing token to this workspace.

        Once bound, every protected write is fenced against the durable epoch
        ledger BEFORE any filesystem mutation (INV-04). When no context is
        bound, writes behave exactly as before (no ownership in play).
        """
        self._ownership = ownership

    def _assert_ownership_before_write(self) -> None:
        """Legacy fence helper (INV-04). See ``_acquire_write_permit``.

        When a live fencing token is bound, it MUST still pass the durable
        epoch/owner check before any mutation. With no token bound this is a
        no-op, preserving the original write behaviour.
        """
        if self._ownership is not None:
            self._ownership.assert_can_write()

    def _acquire_write_permit(self, surface: str | None = None) -> WritePermit | None:
        """Acquire an atomic write permit at the mutation boundary (DF-WO2-002).

        PHASE 6: when a live fencing token bound to a central authority is in
        play, the authority's atomic epoch+owner+lease check runs HERE and HOLDS
        the fence across the mutation, so a concurrent takeover cannot win between
        the check and the side effect (TOCTOU closed). When no ownership is bound,
        or no central authority is wired, this falls back to the durable phase-3
        check (``assert_can_write``) and returns ``None`` — backward compatible.

        DF-WO2-003-full: when ownership is ENABLED but no token is bound this
        fails closed (deny before any mutation) instead of silently writing
        unfenced (INV-06).
        """
        return enforce_write_fence(self._ownership, surface or "local")

    @staticmethod
    def _release_write_permit(permit: WritePermit | None) -> None:
        # Permits are time-bounded holds; nothing to revoke. Kept explicit so a
        # caller can extend this if a revoke protocol is ever added.
        return None

    def write_file(self, path: str | Path, content: str) -> WorkspaceFileResult:
        permit = self._acquire_write_permit()
        try:
            res = self._file_tools.write_text(str(path), content)
        finally:
            self._release_write_permit(permit)
        return WorkspaceFileResult(
            path=res.path,
            content=res.content,
            sha256=res.sha256,
            untrusted=False,
        )

    def read_file(self, path: str | Path, untrusted: bool = False) -> WorkspaceFileResult:
        res = self._file_tools.read_text(str(path), untrusted=untrusted)
        return WorkspaceFileResult(
            path=res.path,
            content=res.content,
            sha256=res.sha256,
            untrusted=res.untrusted,
        )

    def execute_command(
        self,
        command: Sequence[str],
        timeout: int = 30,
        *,
        approved: bool = False,
        correlation_id: str = "",
        task_id: str = "",
    ) -> WorkspaceShellResult:
        permit = self._acquire_write_permit()
        try:
            cmd_list = [str(c) for c in command]
            res = self._shell_tool.run(
                cmd_list,
                approved=approved,
                correlation_id=correlation_id,
                task_id=task_id,
            )
        finally:
            self._release_write_permit(permit)
        return WorkspaceShellResult(
            command=res.command,
            exit_code=res.exit_code,
            stdout=res.stdout,
            stderr=res.stderr,
            untrusted=False,
        )

    def cleanup(self) -> None:
        if self._root_path.exists() and self._root_path.is_dir():
            shutil.rmtree(self._root_path, ignore_errors=True)


class DockerWorkspace(BaseWorkspace):
    """Mock Docker execution workspace. Safe for testing without Docker daemon/network."""

    def __init__(
        self,
        root_path: Path | str = "./workspace",
        image: str = "python:3.12-alpine",
        container_id: str | None = None,
    ) -> None:
        self._root_path = Path(root_path).resolve()
        self.image = image
        self.container_id = container_id or f"mock-container-{uuid.uuid4().hex[:8]}"
        self._storage: dict[str, str] = {}
        self._root_path.mkdir(parents=True, exist_ok=True)
        from antigona.worker.tools.common import WorkspaceGuard
        self._guard = WorkspaceGuard(self._root_path)

    @property
    def backend_type(self) -> str:
        return "docker"

    @property
    def root_path(self) -> Path:
        return self._root_path

    @property
    def is_connected(self) -> bool:
        return True

    def connect(self) -> None:
        return None

    def write_file(self, path: str | Path, content: str) -> WorkspaceFileResult:
        with self._fenced('write_file'):
            rel_path = str(path)
            # Hard workspace binding: resolve strictly inside the root or
            # refuse. ``self._root_path / rel_path`` alone is not enough — if
            # ``rel_path`` is absolute, pathlib discards the left operand and
            # silently writes outside the workspace (e.g. to Downloads).
            full_path = self._guard.resolve(rel_path)
            full_path.parent.mkdir(parents=True, exist_ok=True)
            full_path.write_text(content, encoding="utf-8")
            sha256 = hashlib.sha256(content.encode()).hexdigest()
            self._storage[rel_path] = content
            return WorkspaceFileResult(path=rel_path, content=content, sha256=sha256)

    def read_file(self, path: str | Path, untrusted: bool = False) -> WorkspaceFileResult:
        rel_path = str(path)
        full_path = self._root_path / rel_path
        if full_path.exists():
            content = full_path.read_text(encoding="utf-8")
        elif rel_path in self._storage:
            content = self._storage[rel_path]
        else:
            raise FileNotFoundError(f"File not found in Docker workspace: {path}")
        sha256 = hashlib.sha256(content.encode()).hexdigest()
        return WorkspaceFileResult(
            path=rel_path, content=content, sha256=sha256, untrusted=untrusted
        )

    def execute_command(
        self,
        command: Sequence[str],
        timeout: int = 30,
        *,
        approved: bool = False,
        correlation_id: str = "",
        task_id: str = "",
    ) -> WorkspaceShellResult:
        with self._fenced('execute_command'):
            cmd = [str(c) for c in command]
            stdout = f"[mock docker {self.container_id}] executed: {' '.join(cmd)}"
            return WorkspaceShellResult(
                command=tuple(cmd),
                exit_code=0,
                stdout=stdout,
                stderr="",
            )

    def cleanup(self) -> None:
        if self._root_path.exists() and self._root_path.is_dir():
            shutil.rmtree(self._root_path, ignore_errors=True)


class DockerWorkspaceReal(BaseWorkspace):
    """Real Docker workspace adapter with lazy SDK import and graceful degrade."""

    def __init__(
        self,
        root_path: Path | str = "./workspace",
        image: str = "python:3.12-alpine",
        container_id: str | None = None,
    ) -> None:
        self._root_path = Path(root_path).resolve()
        self.image = image
        self.container_id = container_id
        self._connected = False

    @property
    def backend_type(self) -> str:
        return "docker"

    @property
    def root_path(self) -> Path:
        return self._root_path

    @property
    def is_connected(self) -> bool:
        return self._connected

    def connect(self) -> None:
        try:
            docker = importlib.import_module("docker")
            client = docker.from_env()
            client.ping()
            self._connected = True
        except Exception as exc:
            self._connected = False
            raise WorkspaceConnectionError(f"Docker connection failed: {exc}") from exc

    def write_file(self, path: str | Path, content: str) -> WorkspaceFileResult:
        with self._fenced('write_file'):
            if not self._connected:
                raise WorkspaceConnectionError("Docker workspace is not connected")
            rel_path = str(path)
            sha256 = hashlib.sha256(content.encode()).hexdigest()
            return WorkspaceFileResult(path=rel_path, content=content, sha256=sha256)

    def read_file(self, path: str | Path, untrusted: bool = False) -> WorkspaceFileResult:
        if not self._connected:
            raise WorkspaceConnectionError("Docker workspace is not connected")
        rel_path = str(path)
        return WorkspaceFileResult(path=rel_path, content="", sha256="", untrusted=untrusted)

    def execute_command(
        self,
        command: Sequence[str],
        timeout: int = 30,
        *,
        approved: bool = False,
        correlation_id: str = "",
        task_id: str = "",
    ) -> WorkspaceShellResult:
        with self._fenced('execute_command'):
            if not self._connected:
                raise WorkspaceConnectionError("Docker workspace is not connected")
            cmd = tuple(str(c) for c in command)
            return WorkspaceShellResult(command=cmd, exit_code=0, stdout="", stderr="")

    def cleanup(self) -> None:
        if self._root_path.exists() and self._root_path.is_dir():
            shutil.rmtree(self._root_path, ignore_errors=True)


class SSHWorkspace(BaseWorkspace):
    """Mock SSH execution workspace. Safe for testing without real SSH server."""

    def __init__(
        self,
        root_path: Path | str = "./workspace",
        host: str = "localhost",
        port: int = 22,
        username: str = "root",
        key_path: str | None = None,
    ) -> None:
        self._root_path = Path(root_path).resolve()
        self.host = host
        self.port = port
        self.username = username
        self.key_path = key_path
        self._storage: dict[str, str] = {}
        self._root_path.mkdir(parents=True, exist_ok=True)
        from antigona.worker.tools.common import WorkspaceGuard
        self._guard = WorkspaceGuard(self._root_path)

    @property
    def backend_type(self) -> str:
        return "ssh"

    @property
    def root_path(self) -> Path:
        return self._root_path

    @property
    def is_connected(self) -> bool:
        return True

    def connect(self) -> None:
        return None

    def write_file(self, path: str | Path, content: str) -> WorkspaceFileResult:
        with self._fenced('write_file'):
            rel_path = str(path)
            # Hard workspace binding: resolve strictly inside the root or
            # refuse (see DockerWorkspace.write_file for the pathlib pitfall
            # this closes).
            full_path = self._guard.resolve(rel_path)
            full_path.parent.mkdir(parents=True, exist_ok=True)
            full_path.write_text(content, encoding="utf-8")
            sha256 = hashlib.sha256(content.encode()).hexdigest()
            self._storage[rel_path] = content
            return WorkspaceFileResult(path=rel_path, content=content, sha256=sha256)

    def read_file(self, path: str | Path, untrusted: bool = False) -> WorkspaceFileResult:
        rel_path = str(path)
        full_path = self._root_path / rel_path
        if full_path.exists():
            content = full_path.read_text(encoding="utf-8")
        elif rel_path in self._storage:
            content = self._storage[rel_path]
        else:
            raise FileNotFoundError(f"File not found in SSH workspace: {path}")
        sha256 = hashlib.sha256(content.encode()).hexdigest()
        return WorkspaceFileResult(
            path=rel_path, content=content, sha256=sha256, untrusted=untrusted
        )

    def execute_command(
        self,
        command: Sequence[str],
        timeout: int = 30,
        *,
        approved: bool = False,
        correlation_id: str = "",
        task_id: str = "",
    ) -> WorkspaceShellResult:
        with self._fenced('execute_command'):
            cmd = [str(c) for c in command]
            stdout = f"[mock ssh {self.username}@{self.host}:{self.port}] executed: {' '.join(cmd)}"
            return WorkspaceShellResult(
                command=tuple(cmd),
                exit_code=0,
                stdout=stdout,
                stderr="",
            )

    def cleanup(self) -> None:
        if self._root_path.exists() and self._root_path.is_dir():
            shutil.rmtree(self._root_path, ignore_errors=True)


class SSHWorkspaceReal(BaseWorkspace):
    """Real SSH workspace adapter using paramiko/asyncssh with lazy SDK import."""

    def __init__(
        self,
        root_path: Path | str = "./workspace",
        host: str = "localhost",
        port: int = 22,
        username: str = "root",
        key_path: str | None = None,
    ) -> None:
        self._root_path = Path(root_path).resolve()
        self.host = host
        self.port = port
        self.username = username
        self.key_path = key_path
        self._connected = False

    @property
    def backend_type(self) -> str:
        return "ssh"

    @property
    def root_path(self) -> Path:
        return self._root_path

    @property
    def is_connected(self) -> bool:
        return self._connected

    def connect(self) -> None:
        try:
            paramiko = importlib.import_module("paramiko")
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            client.connect(
                hostname=self.host,
                port=self.port,
                username=self.username,
                key_filename=self.key_path,
                timeout=5,
            )
            self._connected = True
        except Exception as exc:
            self._connected = False
            raise WorkspaceConnectionError(f"SSH connection failed: {exc}") from exc

    def write_file(self, path: str | Path, content: str) -> WorkspaceFileResult:
        with self._fenced('write_file'):
            if not self._connected:
                raise WorkspaceConnectionError("SSH workspace is not connected")
            rel_path = str(path)
            sha256 = hashlib.sha256(content.encode()).hexdigest()
            return WorkspaceFileResult(path=rel_path, content=content, sha256=sha256)

    def read_file(self, path: str | Path, untrusted: bool = False) -> WorkspaceFileResult:
        if not self._connected:
            raise WorkspaceConnectionError("SSH workspace is not connected")
        rel_path = str(path)
        return WorkspaceFileResult(path=rel_path, content="", sha256="", untrusted=untrusted)

    def execute_command(
        self,
        command: Sequence[str],
        timeout: int = 30,
        *,
        approved: bool = False,
        correlation_id: str = "",
        task_id: str = "",
    ) -> WorkspaceShellResult:
        with self._fenced('execute_command'):
            if not self._connected:
                raise WorkspaceConnectionError("SSH workspace is not connected")
            cmd = tuple(str(c) for c in command)
            return WorkspaceShellResult(command=cmd, exit_code=0, stdout="", stderr="")

    def cleanup(self) -> None:
        if self._root_path.exists() and self._root_path.is_dir():
            shutil.rmtree(self._root_path, ignore_errors=True)


class ModalWorkspace(BaseWorkspace):
    """Mock Modal serverless execution workspace. Safe for testing without Modal API keys."""

    def __init__(
        self,
        root_path: Path | str = "./workspace",
        app_name: str = "antigona-workspace",
        environment: str = "dev",
    ) -> None:
        self._root_path = Path(root_path).resolve()
        self.app_name = app_name
        self.environment = environment
        self._storage: dict[str, str] = {}
        self._root_path.mkdir(parents=True, exist_ok=True)
        from antigona.worker.tools.common import WorkspaceGuard
        self._guard = WorkspaceGuard(self._root_path)

    @property
    def backend_type(self) -> str:
        return "modal"

    @property
    def root_path(self) -> Path:
        return self._root_path

    @property
    def is_connected(self) -> bool:
        return True

    def connect(self) -> None:
        return None

    def write_file(self, path: str | Path, content: str) -> WorkspaceFileResult:
        with self._fenced('write_file'):
            rel_path = str(path)
            # Hard workspace binding: resolve strictly inside the root or
            # refuse (see DockerWorkspace.write_file for the pathlib pitfall
            # this closes).
            full_path = self._guard.resolve(rel_path)
            full_path.parent.mkdir(parents=True, exist_ok=True)
            full_path.write_text(content, encoding="utf-8")
            sha256 = hashlib.sha256(content.encode()).hexdigest()
            self._storage[rel_path] = content
            return WorkspaceFileResult(path=rel_path, content=content, sha256=sha256)

    def read_file(self, path: str | Path, untrusted: bool = False) -> WorkspaceFileResult:
        rel_path = str(path)
        full_path = self._root_path / rel_path
        if full_path.exists():
            content = full_path.read_text(encoding="utf-8")
        elif rel_path in self._storage:
            content = self._storage[rel_path]
        else:
            raise FileNotFoundError(f"File not found in Modal workspace: {path}")
        sha256 = hashlib.sha256(content.encode()).hexdigest()
        return WorkspaceFileResult(
            path=rel_path, content=content, sha256=sha256, untrusted=untrusted
        )

    def execute_command(
        self,
        command: Sequence[str],
        timeout: int = 30,
        *,
        approved: bool = False,
        correlation_id: str = "",
        task_id: str = "",
    ) -> WorkspaceShellResult:
        with self._fenced('execute_command'):
            cmd = [str(c) for c in command]
            stdout = f"[mock modal {self.app_name}/{self.environment}] executed: {' '.join(cmd)}"
            return WorkspaceShellResult(
                command=tuple(cmd),
                exit_code=0,
                stdout=stdout,
                stderr="",
            )

    def cleanup(self) -> None:
        if self._root_path.exists() and self._root_path.is_dir():
            shutil.rmtree(self._root_path, ignore_errors=True)


class ModalWorkspaceReal(BaseWorkspace):
    """Real Modal serverless workspace adapter with lazy SDK import."""

    def __init__(
        self,
        root_path: Path | str = "./workspace",
        app_name: str = "antigona-workspace",
        environment: str = "dev",
    ) -> None:
        self._root_path = Path(root_path).resolve()
        self.app_name = app_name
        self.environment = environment
        self._connected = False

    @property
    def backend_type(self) -> str:
        return "modal"

    @property
    def root_path(self) -> Path:
        return self._root_path

    @property
    def is_connected(self) -> bool:
        return self._connected

    def connect(self) -> None:
        try:
            modal = importlib.import_module("modal")
            if hasattr(modal, "config"):
                self._connected = True
            else:
                self._connected = True
        except Exception as exc:
            self._connected = False
            raise WorkspaceConnectionError(f"Modal connection failed: {exc}") from exc

    def write_file(self, path: str | Path, content: str) -> WorkspaceFileResult:
        with self._fenced('write_file'):
            if not self._connected:
                raise WorkspaceConnectionError("Modal workspace is not connected")
            rel_path = str(path)
            sha256 = hashlib.sha256(content.encode()).hexdigest()
            return WorkspaceFileResult(path=rel_path, content=content, sha256=sha256)

    def read_file(self, path: str | Path, untrusted: bool = False) -> WorkspaceFileResult:
        if not self._connected:
            raise WorkspaceConnectionError("Modal workspace is not connected")
        rel_path = str(path)
        return WorkspaceFileResult(path=rel_path, content="", sha256="", untrusted=untrusted)

    def execute_command(
        self,
        command: Sequence[str],
        timeout: int = 30,
        *,
        approved: bool = False,
        correlation_id: str = "",
        task_id: str = "",
    ) -> WorkspaceShellResult:
        with self._fenced('execute_command'):
            if not self._connected:
                raise WorkspaceConnectionError("Modal workspace is not connected")
            cmd = tuple(str(c) for c in command)
            return WorkspaceShellResult(command=cmd, exit_code=0, stdout="", stderr="")

    def cleanup(self) -> None:
        if self._root_path.exists() and self._root_path.is_dir():
            shutil.rmtree(self._root_path, ignore_errors=True)


class DaytonaWorkspace(BaseWorkspace):
    """Mock Daytona execution workspace. Safe for testing without Daytona API keys."""

    def __init__(
        self,
        root_path: Path | str = "./workspace",
        api_key: str | None = None,
        region: str = "eu",
        workspace_id: str | None = None,
    ) -> None:
        self._root_path = Path(root_path).resolve()
        self.api_key = api_key
        self.region = region
        self.workspace_id = workspace_id or f"ws-{uuid.uuid4().hex[:8]}"
        self._storage: dict[str, str] = {}
        self._root_path.mkdir(parents=True, exist_ok=True)
        from antigona.worker.tools.common import WorkspaceGuard
        self._guard = WorkspaceGuard(self._root_path)

    @property
    def backend_type(self) -> str:
        return "daytona"

    @property
    def root_path(self) -> Path:
        return self._root_path

    @property
    def is_connected(self) -> bool:
        return True

    def connect(self) -> None:
        return None

    def write_file(self, path: str | Path, content: str) -> WorkspaceFileResult:
        with self._fenced('write_file'):
            rel_path = str(path)
            # Hard workspace binding: resolve strictly inside the root or
            # refuse (see DockerWorkspace.write_file for the pathlib pitfall
            # this closes).
            full_path = self._guard.resolve(rel_path)
            full_path.parent.mkdir(parents=True, exist_ok=True)
            full_path.write_text(content, encoding="utf-8")
            sha256 = hashlib.sha256(content.encode()).hexdigest()
            self._storage[rel_path] = content
            return WorkspaceFileResult(path=rel_path, content=content, sha256=sha256)

    def read_file(self, path: str | Path, untrusted: bool = False) -> WorkspaceFileResult:
        rel_path = str(path)
        full_path = self._root_path / rel_path
        if full_path.exists():
            content = full_path.read_text(encoding="utf-8")
        elif rel_path in self._storage:
            content = self._storage[rel_path]
        else:
            raise FileNotFoundError(f"File not found in Daytona workspace: {path}")
        sha256 = hashlib.sha256(content.encode()).hexdigest()
        return WorkspaceFileResult(
            path=rel_path, content=content, sha256=sha256, untrusted=untrusted
        )

    def execute_command(
        self,
        command: Sequence[str],
        timeout: int = 30,
        *,
        approved: bool = False,
        correlation_id: str = "",
        task_id: str = "",
    ) -> WorkspaceShellResult:
        with self._fenced('execute_command'):
            cmd = [str(c) for c in command]
            stdout = f"[mock daytona {self.workspace_id}/{self.region}] executed: {' '.join(cmd)}"
            return WorkspaceShellResult(
                command=tuple(cmd),
                exit_code=0,
                stdout=stdout,
                stderr="",
            )

    def cleanup(self) -> None:
        if self._root_path.exists() and self._root_path.is_dir():
            shutil.rmtree(self._root_path, ignore_errors=True)


class DaytonaWorkspaceReal(BaseWorkspace):
    """Real Daytona workspace adapter with lazy SDK import."""

    def __init__(
        self,
        root_path: Path | str = "./workspace",
        api_key: str | None = None,
        region: str = "eu",
        workspace_id: str | None = None,
    ) -> None:
        self._root_path = Path(root_path).resolve()
        self.api_key = api_key
        self.region = region
        self.workspace_id = workspace_id or f"ws-{uuid.uuid4().hex[:8]}"
        self._connected = False

    @property
    def backend_type(self) -> str:
        return "daytona"

    @property
    def root_path(self) -> Path:
        return self._root_path

    @property
    def is_connected(self) -> bool:
        return self._connected

    def connect(self) -> None:
        if not self.api_key:
            self._connected = False
            raise WorkspaceConnectionError("Daytona connection failed: API key unavailable")
        try:
            importlib.import_module("daytona_sdk")
            self._connected = True
        except Exception as exc:
            self._connected = False
            raise WorkspaceConnectionError(f"Daytona connection failed: {exc}") from exc

    def write_file(self, path: str | Path, content: str) -> WorkspaceFileResult:
        with self._fenced('write_file'):
            if not self._connected:
                raise WorkspaceConnectionError("Daytona workspace is not connected")
            rel_path = str(path)
            sha256 = hashlib.sha256(content.encode()).hexdigest()
            return WorkspaceFileResult(path=rel_path, content=content, sha256=sha256)

    def read_file(self, path: str | Path, untrusted: bool = False) -> WorkspaceFileResult:
        if not self._connected:
            raise WorkspaceConnectionError("Daytona workspace is not connected")
        rel_path = str(path)
        return WorkspaceFileResult(path=rel_path, content="", sha256="", untrusted=untrusted)

    def execute_command(
        self,
        command: Sequence[str],
        timeout: int = 30,
        *,
        approved: bool = False,
        correlation_id: str = "",
        task_id: str = "",
    ) -> WorkspaceShellResult:
        with self._fenced('execute_command'):
            if not self._connected:
                raise WorkspaceConnectionError("Daytona workspace is not connected")
            cmd = tuple(str(c) for c in command)
            return WorkspaceShellResult(command=cmd, exit_code=0, stdout="", stderr="")

    def cleanup(self) -> None:
        if self._root_path.exists() and self._root_path.is_dir():
            shutil.rmtree(self._root_path, ignore_errors=True)


class WorkspaceFactory:
    """Factory to instantiate workspace backends based on config or explicit backend type."""

    _registry: dict[str, type[BaseWorkspace]] = {
        "local": LocalWorkspace,
        "docker": DockerWorkspace,
        "ssh": SSHWorkspace,
        "modal": ModalWorkspace,
        "daytona": DaytonaWorkspace,
    }

    _real_registry: dict[str, type[BaseWorkspace]] = {
        "docker": DockerWorkspaceReal,
        "ssh": SSHWorkspaceReal,
        "modal": ModalWorkspaceReal,
        "daytona": DaytonaWorkspaceReal,
    }

    @classmethod
    def register(cls, backend_name: str, workspace_class: type[BaseWorkspace]) -> None:
        cls._registry[backend_name.lower()] = workspace_class

    @classmethod
    def create_workspace(
        cls,
        backend: str | None = None,
        workspace_dir: Path | str | None = None,
        config: Settings | None = None,
        task_id: str | None = None,
        **kwargs: Any,
    ) -> BaseWorkspace:
        raw_backend = (
            backend
            or (config.workspace_backend if config else None)
            or os.getenv("ANTIGONA_WORKSPACE_BACKEND", "local")
            or "local"
        )
        resolved_backend = raw_backend.lower()

        root = workspace_dir or (config.workspace if config else "./workspace")
        root_path = Path(root)
        if task_id:
            root_path = root_path / task_id

        # DF-WO2-003 wiring knobs — popped so they are never forwarded to the backend.
        owner_id = kwargs.pop("owner_id", None)
        ownership_dir = kwargs.pop("ownership_dir", None)
        ownership_lease_seconds = kwargs.pop("ownership_lease_seconds", None)
        kw = dict(kwargs)
        if "workspace_mock" in kw:
            is_mock = bool(kw.pop("workspace_mock"))
        elif config is not None:
            is_mock = config.workspace_mock
        else:
            is_mock = os.getenv("ANTIGONA_WORKSPACE_MOCK", "1") in ("1", "true", "True")

        merged_kwargs = cls._extract_backend_kwargs(resolved_backend, config, **kw)

        cls_type: type[BaseWorkspace] | None = None
        if not is_mock and resolved_backend in cls._real_registry:
            cls_type = cls._real_registry[resolved_backend]

        if cls_type is None:
            cls_type = cls._registry.get(resolved_backend)

        if cls_type is None:
            raise ValueError(
                f"Unknown workspace backend: {resolved_backend}. Available: {list(cls._registry.keys())}"
            )

        ws: BaseWorkspace = cls_type(root_path=root_path, **merged_kwargs)  # type: ignore[call-arg]
        # DF-WO2-003: wire the ownership gate into the live creation path (default off).
        # ``root`` (the base workspace root) is the logical repo anchor, NOT the
        # per-task subdir, so two clones/tasks of the same repo share one authority.
        # When ownership is disabled the helper returns immediately (backward-compat).
        bind_workspace_ownership(
            ws,
            repo_root=root,
            owner_id=owner_id,
            settings=config,
            ownership_dir=ownership_dir,
            lease_seconds=ownership_lease_seconds or 60,
        )
        return ws

    @classmethod
    def _extract_backend_kwargs(
        cls, backend: str, config: Settings | None, **kwargs: Any
    ) -> dict[str, Any]:
        result = dict(kwargs)
        if backend == "ssh":
            if "host" not in result:
                result["host"] = (
                    config.ssh_host if config else os.getenv("ANTIGONA_SSH_HOST", "localhost")
                )
            if "port" not in result:
                result["port"] = (
                    config.ssh_port if config else int(os.getenv("ANTIGONA_SSH_PORT", "22"))
                )
            if "username" not in result:
                result["username"] = (
                    config.ssh_username if config else os.getenv("ANTIGONA_SSH_USERNAME", "root")
                )
            if "key_path" not in result:
                result["key_path"] = (
                    config.ssh_key_path if config else os.getenv("ANTIGONA_SSH_KEY_PATH")
                )
        elif backend == "modal":
            if "app_name" not in result:
                result["app_name"] = (
                    config.modal_app_name
                    if config
                    else os.getenv("ANTIGONA_MODAL_APP_NAME", "antigona-workspace")
                )
            if "environment" not in result:
                result["environment"] = (
                    config.modal_environment
                    if config
                    else os.getenv("ANTIGONA_MODAL_ENVIRONMENT", "dev")
                )
        elif backend == "daytona":
            if "api_key" not in result:
                result["api_key"] = (
                    config.daytona_api_key if config else os.getenv("ANTIGONA_DAYTONA_API_KEY")
                )
            if "region" not in result:
                result["region"] = (
                    config.daytona_region if config else os.getenv("ANTIGONA_DAYTONA_REGION", "eu")
                )
            if "workspace_id" not in result:
                result["workspace_id"] = (
                    config.daytona_workspace_id
                    if config
                    else os.getenv("ANTIGONA_DAYTONA_WORKSPACE_ID")
                )
        elif backend == "docker":
            if "image" not in result:
                result["image"] = (
                    config.docker_image
                    if config
                    else os.getenv("ANTIGONA_DOCKER_IMAGE", "python:3.12-alpine")
                )
        return result
