"""CodexAdapter — delegates tasks to the Codex CLI (codex)."""

from __future__ import annotations

import asyncio
import logging
import shutil
import uuid
from typing import Any

from antigona.subagents.base import ExecutionStatus, SubagentResult

LOGGER = logging.getLogger("antigona.subagents.adapters.codex")


class CodexAdapter:
    """Subagent adapter that invokes ``codex`` CLI via asyncio subprocess.

    The ``codex`` binary must be on $PATH. The adapter spawns a short-lived
    process per execution — no persistent session.
    """

    name: str = "codex"

    def __init__(self, timeout_seconds: int = 300) -> None:
        self.timeout_seconds = timeout_seconds

    @staticmethod
    def available() -> bool:
        """Check if the ``codex`` binary is on $PATH."""
        return shutil.which("codex") is not None

    async def execute(
        self,
        task: str,
        context: dict[str, Any] | None = None,
    ) -> SubagentResult:
        ctx = context or {}
        execution_id = str(uuid.uuid4())
        LOGGER.info(
            "CodexAdapter executing task %s (timeout=%ds)",
            execution_id,
            self.timeout_seconds,
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                "codex",
                task,
                cwd=ctx.get("cwd"),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=ctx.get("env"),
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=self.timeout_seconds,
            )
        except TimeoutError:
            LOGGER.warning("CodexAdapter execution %s timed out", execution_id)
            return SubagentResult(
                execution_id=execution_id,
                status=ExecutionStatus.FAILED,
                output="",
                error=f"Execution timed out after {self.timeout_seconds}s",
                exit_code=-1,
            )
        except FileNotFoundError:
            LOGGER.error("codex binary not found on PATH")
            return SubagentResult(
                execution_id=execution_id,
                status=ExecutionStatus.FAILED,
                output="",
                error="codex binary not found on PATH",
                exit_code=-1,
            )

        exit_code = proc.returncode or 0
        out_text = stdout.decode("utf-8", errors="replace") if stdout else ""
        err_text = stderr.decode("utf-8", errors="replace") if stderr else ""

        status = ExecutionStatus.COMPLETED if exit_code == 0 else ExecutionStatus.FAILED
        return SubagentResult(
            execution_id=execution_id,
            status=status,
            output=out_text,
            error=err_text if err_text else None,
            exit_code=exit_code,
        )

    async def get_status(self, execution_id: str) -> SubagentResult:
        """Adapter runs synchronously per call — status is always terminal."""
        return SubagentResult(
            execution_id=execution_id,
            status=ExecutionStatus.COMPLETED,
            output="",
        )

    async def cancel(self, execution_id: str) -> None:
        LOGGER.info("CodexAdapter cancel %s (no-op for sync adapter)", execution_id)
