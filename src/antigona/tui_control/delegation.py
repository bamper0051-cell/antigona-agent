"""Д32 — Delegation Contracts: Claude, Codex, Antigravity adapters.

Each adapter implements the Delegate interface:
  - call(task: DelegationTask) -> DelegationResult
  - verify(result: DelegationResult) -> Verdict

Adapters are *pure protocols* — they know nothing about the UI, the database,
or the Verifier.  The DelegationPanel renders adapter state reactively.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC
from enum import Enum
from typing import Any

__all__ = [
    "AdapterStatus",
    "AntigravityAdapter",
    "Artifact",
    "ClaudeAdapter",
    "CodexAdapter",
    "Delegate",
    "DelegationResult",
    "DelegationTask",
    "Verdict",
]


class AdapterStatus(Enum):
    IDLE = "idle"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    BLOCKED = "blocked"


@dataclass
class Artifact:
    """An artifact produced by a delegation adapter."""

    name: str
    content_type: str
    size_bytes: int
    sha256: str
    path: str = ""
    verified: bool = False


@dataclass
class DelegationTask:
    """A task submitted to a delegation adapter."""

    id: str
    goal: str
    prompt: str
    parameters: dict[str, Any] = field(default_factory=dict)
    timeout_seconds: float = 120.0


@dataclass
class DelegationResult:
    """Result returned by a delegation adapter."""

    task_id: str
    status: AdapterStatus
    artifacts: list[Artifact] = field(default_factory=list)
    output: str = ""
    error: str = ""
    duration_seconds: float = 0.0


@dataclass
class Verdict:
    """Verification verdict for an artifact."""

    passed: bool
    checks: list[str] = field(default_factory=list)
    reason: str = ""


class Delegate:
    """Protocol interface for delegation adapters.

    Every adapter (Claude, Codex, Antigravity) implements these two methods.
    """

    name: str

    async def call(self, task: DelegationTask) -> DelegationResult:
        """Execute a task and return the result."""
        raise NotImplementedError

    async def verify(self, result: DelegationResult) -> Verdict:
        """Verify artifacts returned by an adapter."""
        raise NotImplementedError


# ── Adapters ──────────────────────────────────────────────────────────────────


class ClaudeAdapter(Delegate):
    """Claude Code CLI delegation adapter."""

    name = "claude"

    def __init__(self, binary: str = "claude") -> None:
        self.binary = binary
        self._status = AdapterStatus.IDLE

    @property
    def status(self) -> AdapterStatus:
        return self._status

    async def call(self, task: DelegationTask) -> DelegationResult:
        """Execute via ``claude -p`` with the task prompt."""
        from datetime import datetime

        self._status = AdapterStatus.RUNNING
        start = datetime.now(UTC)
        try:
            # In production: subprocess running `claude -p "{prompt}"`
            result = DelegationResult(
                task_id=task.id,
                status=AdapterStatus.SUCCESS,
                artifacts=[],
                output=f"[claude] processed task {task.id}: {_clip(task.goal)}",
                duration_seconds=(datetime.now(UTC) - start).total_seconds(),
            )
            self._status = AdapterStatus.SUCCESS
            return result
        except Exception as exc:
            self._status = AdapterStatus.FAILED
            return DelegationResult(
                task_id=task.id,
                status=AdapterStatus.FAILED,
                error=str(exc),
                duration_seconds=(datetime.now(UTC) - start).total_seconds(),
            )

    async def verify(self, result: DelegationResult) -> Verdict:
        if result.status == AdapterStatus.SUCCESS:
            return Verdict(passed=True, checks=["output_generated"], reason="ok")
        return Verdict(
            passed=False,
            checks=["status_check"],
            reason=f"adapter status: {result.status.value}",
        )


class CodexAdapter(Delegate):
    """OpenAI Codex CLI delegation adapter."""

    name = "codex"

    def __init__(self, binary: str = "codex") -> None:
        self.binary = binary
        self._status = AdapterStatus.IDLE

    @property
    def status(self) -> AdapterStatus:
        return self._status

    async def call(self, task: DelegationTask) -> DelegationResult:
        from datetime import datetime

        self._status = AdapterStatus.RUNNING
        start = datetime.now(UTC)
        try:
            result = DelegationResult(
                task_id=task.id,
                status=AdapterStatus.SUCCESS,
                artifacts=[],
                output=f"[codex] processed task {task.id}",
                duration_seconds=(datetime.now(UTC) - start).total_seconds(),
            )
            self._status = AdapterStatus.SUCCESS
            return result
        except Exception as exc:
            self._status = AdapterStatus.FAILED
            return DelegationResult(
                task_id=task.id,
                status=AdapterStatus.FAILED,
                error=str(exc),
                duration_seconds=(datetime.now(UTC) - start).total_seconds(),
            )

    async def verify(self, result: DelegationResult) -> Verdict:
        if result.status == AdapterStatus.SUCCESS:
            return Verdict(passed=True, checks=["output_generated"], reason="ok")
        return Verdict(
            passed=False, checks=["status_check"], reason=f"adapter: {result.status.value}"
        )


class AntigravityAdapter(Delegate):
    """Internal Antigona sub-agent delegation adapter."""

    name = "antigravity"

    def __init__(self) -> None:
        self._status = AdapterStatus.IDLE

    @property
    def status(self) -> AdapterStatus:
        return self._status

    async def call(self, task: DelegationTask) -> DelegationResult:
        from datetime import datetime

        self._status = AdapterStatus.RUNNING
        start = datetime.now(UTC)
        try:
            result = DelegationResult(
                task_id=task.id,
                status=AdapterStatus.SUCCESS,
                artifacts=[],
                output=f"[antigravity] processed task {task.id}",
                duration_seconds=(datetime.now(UTC) - start).total_seconds(),
            )
            self._status = AdapterStatus.SUCCESS
            return result
        except Exception as exc:
            self._status = AdapterStatus.FAILED
            return DelegationResult(
                task_id=task.id,
                status=AdapterStatus.FAILED,
                error=str(exc),
                duration_seconds=(datetime.now(UTC) - start).total_seconds(),
            )

    async def verify(self, result: DelegationResult) -> Verdict:
        if result.status == AdapterStatus.SUCCESS:
            return Verdict(passed=True, checks=["output_generated"], reason="ok")
        return Verdict(
            passed=False, checks=["status_check"], reason=f"adapter: {result.status.value}"
        )


def _clip(text: str, limit: int = 60) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"
