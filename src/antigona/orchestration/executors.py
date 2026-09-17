import asyncio
import json
import logging
import os
import shlex
import subprocess
import tempfile
import time
import typing
from pathlib import Path
from typing import Any

__all__ = ["ExecResult", "ServiceExecutors", "asyncio", "json", "shlex", "typing", "Any"]


from antigona.orchestration.autonomy import AutonomyContractError, WorkspaceBoundary
from antigona.orchestration.router import ServiceCapability, service_has_capabilities
from antigona.orchestration.state import FailureClass

logger = logging.getLogger(__name__)


class ExecResult:
    def __init__(
        self,
        ok: bool,
        output: str,
        failure_class: str,
        duration_s: float,
        service_id: str,
    ) -> None:
        self.ok = ok
        self.output = output
        self.failure_class = failure_class
        self.duration_s = duration_s
        self.service_id = service_id


class ServiceExecutors:
    def __init__(self, timeout: int = 120) -> None:
        self.timeout = timeout

    def execute(
        self,
        service_id: str,
        instruction: str,
        timeout: int | None = None,
        *,
        workspace: str | None = None,
        writable: bool = False,
    ) -> ExecResult:
        """Run the instruction through the service CLI:
          'claude'  -> claude -p <instruction>        (capture stdout)
          'codex'   -> codex exec --skip-git-repo-check <instruction>
          'agy'     -> agy -p <instruction>
          'grok'    -> grok --prompt-file <tmpfile> --no-plan --no-subagents --no-memory -m grok-4.5
          'hermes'  -> ExecResult(ok=False, failure_class=INVALID_OUTPUT) —
                       Hermes is orchestrator-only; goal tasks must never
                       "succeed" through an internal stub (false DONE).
        Use subprocess.run with timeout; on TimeoutExpired -> failure_class=TIMEOUT;
        on CalledProcessError/returncode!=0 -> classify by stderr keywords:
          'rate limit'/'rate_limit'/'429' -> RATE_LIMIT
          'quota'/'insufficient_quota' -> QUOTA_EXHAUSTED
          'auth'/'401'/'unauthorized' -> AUTH_FAILURE
          'context'/'token limit' -> CONTEXT_LIMIT
          else -> PROCESS_CRASH
        Empty/whitespace output with rc=0 -> INVALID_OUTPUT.
        Returns ExecResult with duration and failure_class ('' on success)."""
        eff_timeout = timeout if timeout is not None else self.timeout

        if writable and not service_has_capabilities(
            service_id, frozenset({ServiceCapability.WRITE_WORKSPACE})
        ):
            return ExecResult(
                ok=False,
                output=f"service {service_id} cannot write the workspace",
                failure_class=FailureClass.CAPABILITY_MISMATCH.value,
                duration_s=0.0,
                service_id=service_id,
            )

        if service_id == "hermes":
            # Hermes is the orchestrator/planner, NOT an execution service.
            # Auto-success here would mint false DONE evidence (Grok audit
            # CRITICAL). If a goal task somehow routes here, it fails loudly.
            return ExecResult(
                ok=False,
                output="hermes is orchestrator-only; goal execution must run on a service",
                failure_class=FailureClass.INVALID_OUTPUT.value,
                duration_s=0.0,
                service_id=service_id,
            )

        tmp_file_path: str | None = None
        if service_id == "claude":
            cmd = ["claude", "-p", "--no-session-persistence", instruction]
            if writable:
                cmd += [
                    "--permission-mode", "acceptEdits",
                    "--allowedTools",
                    "Read,Glob,Grep,Edit,Write,Bash(pytest *),Bash(python -m pytest *),Bash(git diff *)",
                ]
            else:
                cmd += ["--allowedTools", "Read,Glob,Grep"]
        elif service_id == "codex":
            cmd = ["codex", "exec", "--skip-git-repo-check"]
            if writable:
                cmd += ["--sandbox", "workspace-write"]
            else:
                cmd += ["--sandbox", "read-only"]
            cmd.append(instruction)
        elif service_id == "agy":
            cmd = ["agy", "-p", instruction]
        elif service_id == "grok":
            tmp_fd, tmp_file_path = tempfile.mkstemp(prefix="grok_prompt_", text=True)
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                f.write(instruction)
            cmd = [
                "grok",
                "-p",
                instruction,
                "--no-plan",
                "--no-subagents",
                "--no-memory",
            ]
        else:
            cmd = [service_id, instruction]

        start_t = time.monotonic()
        try:
            if workspace:
                confined = WorkspaceBoundary(
                    Path(workspace), writable=writable
                ).run(
                    cmd,
                    timeout=eff_timeout,
                    extra_env={
                        key: os.environ[key]
                        for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY")
                        if key in os.environ
                    },
                )
                res = subprocess.CompletedProcess(
                    cmd, confined.returncode, confined.stdout, confined.stderr
                )
            else:
                res = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=eff_timeout,
                    check=False,
                    input="",  # codex exec waits on stdin without it
                )
            duration_s = time.monotonic() - start_t
            stdout = res.stdout or ""
            stderr = res.stderr or ""

            if res.returncode != 0:
                failure_class = self._classify_error(stderr)
                return ExecResult(
                    ok=False,
                    output=stdout or stderr,
                    failure_class=failure_class.value,
                    duration_s=duration_s,
                    service_id=service_id,
                )

            if not stdout.strip():
                return ExecResult(
                    ok=False,
                    output="",
                    failure_class=FailureClass.INVALID_OUTPUT.value,
                    duration_s=duration_s,
                    service_id=service_id,
                )

            return ExecResult(
                ok=True,
                output=stdout,
                failure_class="",
                duration_s=duration_s,
                service_id=service_id,
            )

        except AutonomyContractError as exc:
            return ExecResult(
                ok=False,
                output=str(exc),
                failure_class=FailureClass.INVALID_OUTPUT.value,
                duration_s=time.monotonic() - start_t,
                service_id=service_id,
            )
        except subprocess.TimeoutExpired as exc:
            duration_s = time.monotonic() - start_t
            output = ""
            if exc.stdout:
                output = exc.stdout.decode() if isinstance(exc.stdout, bytes) else str(exc.stdout)
            elif exc.stderr:
                output = exc.stderr.decode() if isinstance(exc.stderr, bytes) else str(exc.stderr)
            return ExecResult(
                ok=False,
                output=output,
                failure_class=FailureClass.TIMEOUT.value,
                duration_s=duration_s,
                service_id=service_id,
            )
        finally:
            if tmp_file_path and os.path.exists(tmp_file_path):
                try:
                    os.remove(tmp_file_path)
                except OSError:
                    pass

    def _classify_error(self, stderr: str) -> FailureClass:
        stderr_lower = stderr.lower()
        if any(kw in stderr_lower for kw in ("rate limit", "rate_limit", "429")):
            return FailureClass.RATE_LIMIT
        if any(kw in stderr_lower for kw in ("quota", "insufficient_quota")):
            return FailureClass.QUOTA_EXHAUSTED
        if any(kw in stderr_lower for kw in ("auth", "401", "unauthorized")):
            return FailureClass.AUTH_FAILURE
        if any(kw in stderr_lower for kw in ("context", "token limit")):
            return FailureClass.CONTEXT_LIMIT
        return FailureClass.PROCESS_CRASH

    def simulate(self, service_id: str, failure_class: str) -> ExecResult:
        """Test hook: pretend the service failed with the given class
        (used by E2E-4/E2E-5 controlled failover tests; not called in prod path)."""
        ok = not bool(failure_class)
        return ExecResult(
            ok=ok,
            output=f"simulated failure: {failure_class}" if failure_class else "simulated success",
            failure_class=failure_class,
            duration_s=0.001,
            service_id=service_id,
        )
