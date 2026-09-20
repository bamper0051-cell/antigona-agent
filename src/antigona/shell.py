from __future__ import annotations

import subprocess
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path

from .contracts import Evidence, ToolResult
from .observability import event
from .ownership.epoch import FenceDeniedError, OwnershipContext
from .ownership.wiring import enforce_write_fence
from .result_safety import sanitize_result_text
from .sandbox.docker_sandbox import image_missing_message, image_missing_signal
from .sandbox.runner import (
    DEFAULT_RUNTIME,
    SandboxIsolationError,
    SandboxProfile,
    build_run_argv,
    ensure_runtime_available,
    resolve_workspace_uid_gid,
)
from .tools.shell_command import (
    normalize_apt_sandbox_user,
    normalize_shell_command_first_token,
    normalize_system_package_install,
    strip_shell_tool_prefix_argv,
    to_shell_argv,
)


@dataclass(frozen=True)
class ShellInput:
    command: tuple[str, ...]
    execution_id: str | None = None


class DockerShellTool:
    name = "sandbox.shell"
    description = "Run argv without a host shell in Docker"
    risk_level = "high"
    requires_approval = True
    sandbox_required = True

    def __init__(
        self,
        workspace: Path,
        image: str = "python:3.12-alpine",
        timeout_seconds: int = 30,
        output_cap: int = 65_536,
        max_workspace_bytes: int = 10_000_000,
        runtime: str = DEFAULT_RUNTIME,
        network: str = "none",
        read_only: bool = True,
        caps_add: tuple[str, ...] = (),
        memory: str = "128m",
        ownership: OwnershipContext | None = None,
    ) -> None:
        self.workspace = workspace.resolve()
        self.workspace.mkdir(parents=True, exist_ok=True, mode=0o750)
        self.workspace.chmod(0o750)
        self.image = image
        self.timeout_seconds = timeout_seconds
        self.output_cap = output_cap
        self.max_workspace_bytes = max_workspace_bytes
        self.runtime = runtime
        self._process: subprocess.Popen[bytes] | None = None
        self._container_name: str | None = None
        self._cancelled = False
        self._lock = threading.Lock()
        self.ownership = ownership
        uid, gid = resolve_workspace_uid_gid(self.workspace)
        self._profile = SandboxProfile(
            workspace=self.workspace,
            image=image,
            runtime=runtime,
            network=network,
            read_only=read_only,
            caps_add=caps_add,
            memory=memory,
            uid=uid,
            gid=gid,
        )

    def bind_ownership(self, ownership: OwnershipContext | None) -> None:
        """Bind a live fencing token to this tool (DF-WO2-003-full).

        Lets the canonical ``bind_workspace_ownership`` /
        ``mint_execution_ownership`` helpers fence the sandbox shell surface the
        same way they fence a workspace object.  ``None`` unbinds (ownership
        disabled / token dropped after a single action).
        """
        self.ownership = ownership

    def _size(self) -> int:
        return sum(path.stat().st_size for path in self.workspace.rglob("*") if path.is_file())

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True
            if self._container_name:
                try:
                    subprocess.run(
                        ["docker", "kill", self._container_name],
                        capture_output=True,
                        timeout=5,
                        check=False,
                    )
                except Exception:
                    pass
            if self._process and self._process.poll() is None:
                try:
                    self._process.terminate()
                except Exception:
                    pass

    def cancel_execution(self, execution_id: str) -> None:
        name = f"antigona-{execution_id[:32]}"
        try:
            subprocess.run(
                ["docker", "kill", name],
                capture_output=True,
                timeout=5,
                check=False,
            )
        except Exception:
            pass
        self.cancel()

    @staticmethod
    def _failure(error: str, *, retryable: bool = False) -> ToolResult:
        return ToolResult(False, "failed", error=error, retryable=retryable)

    def _recover_named_execution(self, name: str) -> ToolResult | None:
        try:
            existing = subprocess.run(
                ["docker", "inspect", name],
                capture_output=True,
                check=False,
            )
            if existing.returncode != 0:
                return None
            subprocess.run(
                ["docker", "wait", name],
                capture_output=True,
                timeout=self.timeout_seconds,
                check=False,
            )
            # Inspect actual exit code
            exit_code_res = subprocess.run(
                ["docker", "inspect", "--format={{.State.ExitCode}}", name],
                capture_output=True,
                text=True,
                check=False,
            )
            exit_code = exit_code_res.stdout.strip() if exit_code_res.returncode == 0 else ""
            if exit_code != "0":
                return self._failure(f"recovered shell exited with code {exit_code or 'unknown'}", retryable=False)

            # Retrieve stdout logs
            logs_res = subprocess.run(
                ["docker", "logs", name],
                capture_output=True,
                text=True,
                check=False,
            )
            output = sanitize_result_text(
                logs_res.stdout or "",
                max_length=self.output_cap,
                # FP-L23R: the container's stdout IS the effect trace — its line
                # structure must survive the tool boundary. Flattening it here
                # (the historical default) glued a two-line ``ls | head -2``
                # into one 72-character token that no later layer could
                # un-flatten, so the durable artifact, the verifier and the
                # owner all saw an opaque blob. Line breaks are not a secret;
                # redaction and the output cap still apply.
                preserve_newlines=True,
            )
            return ToolResult(True, "completed", {"output": output or ""})
        except FileNotFoundError:
            return self._failure("sandbox unavailable")
        except subprocess.TimeoutExpired:
            return self._failure("existing operation still running", retryable=True)
        except Exception:
            return self._failure("sandbox unavailable")


    def execute(self, arguments: ShellInput) -> ToolResult:
        if not arguments.command:
            return self._failure("empty command")
        # Strip literal tool prefix (shell:, bash:, sh:, execute:) before argv processing (W6b-2)
        norm_cmd = strip_shell_tool_prefix_argv(arguments.command)
        if not norm_cmd:
            return self._failure("empty command")
        arguments = ShellInput(command=norm_cmd, execution_id=arguments.execution_id)
        # DF-WO2-003-full: a shell command can write the workspace, so it must
        # pass the ownership fence BEFORE anything runs.  When ownership is
        # disabled this is a no-op (backward-compat); when enabled, a
        # stale/unauthorised owner's execution is DENIED (fail-closed, INV-06).
        try:
            enforce_write_fence(self.ownership, "sandbox.shell")
        except FenceDeniedError as exc:
            return self._failure(f"protected execution denied: {exc.check.reason}")
        # Fail-closed isolation gate (S-ISO-1): re-verify the runtime Docker can
        # ACTUALLY launch right now, BEFORE any container is created.  gVisor OR
        # refusal — never a silent downgrade to the shared host kernel.
        try:
            ensure_runtime_available(self.runtime)
        except SandboxIsolationError as exc:
            event(
                "sandbox_isolation_refused",
                service="sandbox",
                correlation_id=None,
                status="refused",
                runtime=self.runtime,
                reason=str(exc)[:1000],
            )
            return self._failure(str(exc))
        except Exception:
            return self._failure(
                "sandbox isolation check failed — refusing to run (fail-closed)"
            )
        # Case-insensitive leading-token normalization (UX): users commonly
        # type "Apt"/"Pwd"/"Ls"; /bin/sh inside the container is
        # case-sensitive, so fold only the first command token to lowercase
        # (arguments left untouched). See tools/shell_command.py.
        _norm = list(arguments.command)
        if _norm:
            _norm[0] = normalize_shell_command_first_token(str(_norm[0]))
        arguments = ShellInput(command=tuple(_norm), execution_id=arguments.execution_id)
        # FP-L03c: a SINGLE packed element is the container shell's command
        # LINE, not an argv.  It is therefore ALWAYS executed as
        # ``/bin/sh -c <line>`` — no operator sniffing, no ``shlex.split``, no
        # re-quoting — so ``$VAR``, ``$((...))``, globs, substitutions,
        # redirections and ``sh`` aliases behave as the user expects inside the
        # container.  A real argv (several elements) is passed through exactly
        # as the caller gave it.
        arguments = ShellInput(
            command=to_shell_argv(arguments.command),
            execution_id=arguments.execution_id,
        )
        arguments = ShellInput(command=normalize_system_package_install(arguments.command, alpine="alpine" in self.image.lower()), execution_id=arguments.execution_id)
        # apt/apt-get under --cap-drop=ALL cannot drop to its "_apt" sandbox
        # user (no CAP_SETGID/SETUID on arbitrary targets after drop) and the
        # partial file chown/chmod then fails, corrupting the package index
        # ("Unable to locate package"). Disabling apt's privilege-dropping
        # sandbox lets it run as the container user (root, with DAC_OVERRIDE)
        # which can write /var/lib/apt. Safe in a throwaway, non-privileged
        # container. Only touched for apt/apt-get; everything else is untouched.
        # FP-L03c: an ``apt`` command packed into one element is now a shell
        # LINE, so the flag is inserted into the line itself — the workaround
        # must survive the switch to ``/bin/sh -c``.
        arguments = ShellInput(
            command=normalize_apt_sandbox_user(arguments.command),
            execution_id=arguments.execution_id,
        )
        try:
            if self._size() > self.max_workspace_bytes:
                return self._failure("workspace quota exceeded")
        except Exception:
            return self._failure("workspace unavailable")

        name = (
            f"antigona-{arguments.execution_id[:32]}"
            if arguments.execution_id
            else f"antigona-{uuid.uuid4().hex}"
        )
        if arguments.execution_id:
            recovered = self._recover_named_execution(name)
            if recovered is not None:
                return recovered


        try:
            command = build_run_argv(self._profile, arguments.command, name=name)
        except Exception:
            return self._failure("sandbox policy rejected command")

        try:
            with self._lock:
                self._cancelled = False
                self._container_name = name
                self._process = subprocess.Popen(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
            stdout, _stderr = self._process.communicate(timeout=self.timeout_seconds)
            returncode = self._process.returncode
        except FileNotFoundError:
            return self._failure("sandbox unavailable")
        except subprocess.TimeoutExpired:
            self.cancel()
            return self._failure("tool timeout", retryable=True)
        except Exception:
            return self._failure("tool execution failed")
        finally:
            with self._lock:
                self._process = None
                self._container_name = None

        if self._cancelled:
            return ToolResult(False, "cancelled", error="tool cancelled")
        try:
            if self._size() > self.max_workspace_bytes:
                return self._failure("workspace quota exceeded after execution")
        except Exception:
            return self._failure("workspace unavailable")
        if returncode != 0:
            raw_err = _stderr.decode(errors="replace")
            # F-20260919T0230Z: docker exits 125 when the image is absent and its
            # automatic pull was refused by the sandbox socket proxy (by design).
            # That raw docker text must NOT be surfaced as the task answer; name
            # the image and the remedy instead.  Narrow: 125 + a specific marker
            # only — a genuine command failure keeps its real output below.
            if image_missing_signal(returncode, raw_err):
                event(
                    "sandbox_image_missing",
                    service="sandbox",
                    correlation_id=None,
                    status="blocked",
                    image=self.image,
                    exit_code=returncode,
                    reason=(
                        "docker run exit 125 with an image-missing/refused-pull "
                        "stderr; the socket proxy refuses pulls by design"
                    ),
                )
                return ToolResult(
                    False,
                    "failed",
                    error=image_missing_message(self.image),
                    retryable=False,
                    evidence=[
                        Evidence("returncode", str(returncode)),
                        Evidence("diagnostic", "sandbox_image_missing"),
                        Evidence("image", self.image),
                    ],
                )
            text = raw_err.lower()
            diagnostic = (
                "command_not_found"
                if ("not found" in text or "no such file" in text)
                else "permission_denied"
                if "permission denied" in text
                else "package_not_found"
                if ("unable to locate" in text or "no matching package" in text)
                else "unknown"
            )
            sanitized_err = sanitize_result_text(raw_err, max_length=200) or ""
            err_line = next((line.strip() for line in sanitized_err.splitlines() if line.strip()), "")
            if any(
                k in err_line.casefold()
                for k in (
                    ".env",
                    ".key",
                    ".pem",
                    "password",
                    "secret",
                    "bearer",
                    "token",
                    "synthetic-password",
                )
            ):
                err_line = ""
            diag_label = (
                f"{diagnostic} (not found)"
                if diagnostic == "command_not_found"
                else diagnostic
            )
            if err_line:
                error_msg = (
                    f"tool exited non-zero (exit code {returncode}, {diag_label}): {err_line}"
                )
            else:
                error_msg = f"tool exited non-zero (exit code {returncode}, {diag_label})"
            return ToolResult(
                False,
                "failed",
                error=error_msg,
                evidence=[
                    Evidence("returncode", str(returncode)),
                    Evidence("diagnostic", diagnostic),
                ],
            )

        # The complete stdout stream is redacted before any cap is applied.  stderr
        # remains a separate transient stream and is never exposed, even on failure —
        # it is untrusted, sandboxed-command-controlled content.
        output = sanitize_result_text(
            stdout.decode(errors="replace"),
            max_length=self.output_cap,
            # FP-L23R (root cause): the shell tool was the FIRST lossy layer.
            # ``sanitize_result_text`` defaults to ``preserve_newlines=False``,
            # so the real stdout lost its lines here — before
            # ``project_tool_result``, before ``_materialize_stdout_artifact``
            # and before the verifier. The FP-L23 fix downstream was therefore
            # dead on the live path: it faithfully re-sanitized an
            # already-glued string. Keeping the line structure here is safe
            # (a newline is not a secret; redaction + the output cap are
            # unchanged) and is what makes the artifact, the judge's effect
            # facts and the owner's message faithful again.
            preserve_newlines=True,
        )
        return ToolResult(True, "completed", {"output": output or ""})
