"""Isolated ephemeral Docker sandbox for owner-approved high-risk shell commands.

Security contract (owner directive, 2026-08-10):
  * ephemeral container (``--rm``) — never reused, never left behind;
  * NO ``--privileged``, NO host network (default isolated bridge), NO Docker
    socket mount, NO arbitrary host mounts;
  * the ONLY host bind is the minimal workspace mount (``-v <ws>:/workspace``);
  * resource limits (memory + CPUs) and a hard timeout via ``--stop-timeout``;
  * audit logging carries the correlation_id / task_id through every event;
  * fail-closed: if the Docker runtime is unavailable the caller must refuse to
    run high-risk commands on the host (never a fallback).

The high-risk command is executed by the container's own shell so shell
semantics are preserved; the container never shares the host's filesystem,
network, or devices.
"""

from __future__ import annotations

import logging
import shlex
import subprocess
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from antigona.observability import event
from antigona.sandbox.runner import (
    DEFAULT_RUNTIME,
    ISOLATION_REFUSED,
    SandboxIsolationError,
    ensure_runtime_available,
    resolve_runtime,
)
from antigona.tools.shell_command import SHELL_ARGV_PREFIX
from antigona.workspace import ToolError

LOGGER = logging.getLogger("antigona")

# Defaults matching config.docker_image / worker sandbox settings.
DEFAULT_IMAGE = "python:3.12-alpine"
DEFAULT_TIMEOUT = 60
DEFAULT_MEMORY = "512m"
DEFAULT_CPUS = 1.0
WORKSPACE_MOUNT_TARGET = "/workspace"

# Paths that are NEVER allowed to be bind-mounted into the sandbox.
_FORBIDDEN_HOST_PATHS = frozenset({"/", "/var/run/docker.sock", "/run/docker.sock"})

#: ``docker run`` exit code for a failure of DOCKER ITSELF (daemon/runtime/image
#: problems), as opposed to the exit code of the container's own command, which
#: docker passes through unchanged (126/127 have their own meanings).
DOCKER_RUN_FAILURE_EXIT = 125

#: Substrings that identify the *image is not present locally* failure
#: specifically.  Detection is deliberately narrow: exit 125 ALONE is NOT the
#: signal — a dead daemon, a rejected flag or an OCI-runtime failure also exits
#: 125 and MUST keep its real output.  The last marker is the sandbox
#: docker-socket proxy's own 403 body (``deploy/sandbox/docker_socket_proxy.py``
#: ``_deny``: "request not allowed by allowlist"), which is exactly what the
#: docker CLI receives when ``docker run`` tries its automatic
#: ``POST /images/create`` pull through the sandbox socket.
_IMAGE_MISSING_MARKERS: tuple[str, ...] = (
    "no such image",
    "unable to find image",
    "pull access denied",
    "requested access to the resource is denied",
    "not allowed by allowlist",
)


def image_missing_marker(text: str) -> bool:
    """True when *text* is docker's "this image does not exist" message.

    Used both for the read-only ``docker image inspect`` diagnostic probe
    (rc != 0 + ``No such image``) and for the post-run stderr classification.
    """
    lowered = (text or "").lower()
    return any(marker in lowered for marker in _IMAGE_MISSING_MARKERS)


def image_missing_signal(exit_code: int, stderr: str) -> bool:
    """True when *stderr* is docker's image-missing failure and not a command failure."""
    return exit_code == DOCKER_RUN_FAILURE_EXIT and image_missing_marker(stderr)


def image_missing_message(image: str) -> str:
    """Product-level failure text for a missing sandbox image.

    Names the image, states the boundary (the sandbox socket proxy refuses
    pulls BY DESIGN) and gives the exact remedy.  No weaker sandbox and no
    host fallback are offered.
    """
    return (
        f"sandbox image {image!r} is not present on the Docker host, and it cannot be "
        f"pulled from inside the sandbox: the sandbox docker-socket proxy refuses "
        f"image pulls by design (POST /images/create is not in its allowlist), so the "
        f"automatic pull `docker run` attempts is denied. Remedy: pull the image on "
        f"the Docker host using the real docker socket, i.e. `docker pull {image}`, "
        f"then retry. Refusing to run the command (fail-closed — no weaker sandbox, "
        f"no host fallback)."
    )


class DockerSandboxUnavailableError(ToolError):
    """Raised when the Docker runtime cannot service a sandboxed run."""


@dataclass(frozen=True)
class DockerSandboxResult:
    command: tuple[str, ...]
    exit_code: int
    stdout: str
    stderr: str
    container_id: str
    timed_out: bool = False


class DockerSandboxBackend:
    """Run a command inside a throwaway, locked-down Docker container."""

    def __init__(
        self,
        image: str = DEFAULT_IMAGE,
        *,
        timeout_seconds: int = DEFAULT_TIMEOUT,
        memory: str = DEFAULT_MEMORY,
        cpus: float = DEFAULT_CPUS,
        workspace: str | Path | None = None,
        docker_binary: str = "docker",
        runtime: str | None = None,
    ) -> None:
        self.image = image
        self.timeout_seconds = timeout_seconds
        self.memory = memory
        self.cpus = cpus
        self.workspace = Path(workspace).resolve() if workspace else None
        self.docker_binary = docker_binary
        #: OCI runtime.  ``None`` = not yet resolved; the resolve+verify happens
        #: at ``run()`` time so the isolation level is always probed live and a
        #: missing gVisor refuses execution instead of defaulting to the host
        #: kernel.  Never left to the Docker daemon default.
        self.runtime = runtime

    def is_available(self) -> bool:
        """Return True only if the Docker CLI is present and the daemon answers."""
        if shutil_which(self.docker_binary) is None:
            return False
        try:
            probe = subprocess.run(
                [self.docker_binary, "info"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            return probe.returncode == 0
        except Exception:
            return False

    def _image_present(self) -> bool | None:
        """Read-only image-presence probe: ``docker image inspect <image>``.

        ``True`` — Docker confirms the image exists locally;
        ``False`` — Docker says it does not (``No such image``);
        ``None``  — undetermined (CLI/daemon problem, timeout).

        This NEVER pulls: the proxy deliberately only admits the read
        (``GET /images/<name>/json``).

        The result is DIAGNOSTIC ONLY and must never gate execution: a locally
        absent image can still be obtained by a pull-capable daemon on
        ``docker run``.  When the run's pull IS refused, the post-run
        translation (rc 125 + image marker) raises the product-level
        fail-closed error.
        """
        try:
            probe = subprocess.run(
                [self.docker_binary, "image", "inspect", self.image],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except Exception:
            return None
        if probe.returncode == 0:
            return True
        if image_missing_marker(probe.stderr or ""):
            return False
        return None

    def _build_argv(self, command: Sequence[str]) -> list[str]:
        cmd = [str(c) for c in command]
        if not cmd:
            raise ToolError("sandbox command must not be empty")
        if len(cmd) == 3 and tuple(cmd[:2]) == SHELL_ARGV_PREFIX:
            # FP-L03c: the command is already ``["/bin/sh", "-c", <line>]``, so
            # *line* is a container-side command LINE that the container's own
            # shell must receive byte-for-byte.  ``shlex.join`` re-quoted it
            # (``echo '$((2+2))'``), which turned expansions, substitutions and
            # globs into literals, and quoting the whole line also made docker
            # look for an executable literally named ``/bin/sh -c ...`` (127).
            # No host path can be reached from that line: the container has no
            # host filesystem — the single bind mount is the workspace.
            shell_line = cmd[2]
            if not shell_line.strip():
                raise ToolError("sandbox command must not be empty")
            if "\x00" in shell_line:
                raise ToolError("sandbox command must not contain NUL bytes")
        else:
            # Never let an absolute host path leak into the container.
            if any(tok.startswith("/") for tok in cmd):
                raise ToolError("absolute paths are forbidden in sandboxed commands")
            shell_line = shlex.join(cmd)
        argv = [
            self.docker_binary,
            "run",
            "--rm",
            # --runtime is ALWAYS explicit so gVisor vs runc is never left to a
            # daemon default (a default of runc is a silent kernel downgrade).
            f"--runtime={self.runtime or DEFAULT_RUNTIME}",
            "--name",
            f"antigona-sandbox-{uuid.uuid4().hex[:12]}",
            "--network", "bridge",  # isolated default network — NO host network
            "--security-opt", "no-new-privileges",
            "--memory", self.memory,
            "--cpus", f"{self.cpus:.2f}",
            "--stop-timeout", f"{self.timeout_seconds}",
            "--workdir", WORKSPACE_MOUNT_TARGET,
        ]
        if self.workspace is not None:
            argv += ["--volume", f"{self.workspace}:{WORKSPACE_MOUNT_TARGET}"]
        argv += [self.image, "/bin/sh", "-c", shell_line]
        return argv

    def run(
        self,
        command: Sequence[str],
        *,
        correlation_id: str = "",
        task_id: str = "",
    ) -> DockerSandboxResult:
        if not self.is_available():
            event(
                "docker_sandbox_unavailable",
                service="sandbox",
                correlation_id=correlation_id or None,
                task_id=task_id or None,
                status="blocked",
                reason="docker runtime unavailable — fail closed, no host fallback",
            )
            raise DockerSandboxUnavailableError(
                "Docker sandbox is unavailable — refusing high-risk command (fail-closed, "
                "no host fallback)"
            )

        # Fail-closed isolation gate: resolve the runtime ONCE, then re-verify it
        # is launchable right now.  gVisor OR refusal — never a silent downgrade.
        if self.runtime is None:
            try:
                self.runtime = resolve_runtime()
            except SandboxIsolationError:
                self.runtime = ISOLATION_REFUSED
        try:
            ensure_runtime_available(self.runtime)
        except SandboxIsolationError as exc:
            event(
                "sandbox_isolation_refused",
                service="sandbox",
                correlation_id=correlation_id or None,
                task_id=task_id or None,
                status="refused",
                runtime=self.runtime,
                reason=str(exc)[:1000],
            )
            raise

        # Image-presence probe — DIAGNOSTIC ONLY, never a refusal gate.
        #
        # A locally absent image is NOT proof that the image cannot be obtained:
        # a pull-capable daemon (a GitHub Actions runner, any host with the real
        # docker socket) fetches it on ``docker run`` via its automatic
        # ``POST /images/create``.  Refusing here broke exactly that legitimate
        # path (B1-20260919T0652Z_IMAGE_PREFLIGHT_REGRESSION: the sandbox E2E
        # nodes failed with "image ... is not present" on a runner that could
        # simply have pulled it).
        #
        # So execution ALWAYS proceeds to ``docker run``.  The probe survives
        # only to record an audit event.  The fail-closed product error is
        # produced by the POST-RUN translation below — rc 125 + an image-missing
        # marker — which fires exactly when the image really could not be
        # obtained (the socket proxy denies the pull by design).
        if self._image_present() is False:
            event(
                "docker_sandbox_image_absent_locally",
                service="sandbox",
                correlation_id=correlation_id or None,
                task_id=task_id or None,
                image=self.image,
                status="diagnostic",
                reason=(
                    "sandbox image not present locally (docker image inspect: No such "
                    "image); proceeding to docker run so a pull-capable daemon can "
                    "obtain it — a refused pull is translated after the run"
                ),
            )

        argv = self._build_argv(command)
        container_id = next((v for v in argv if v.startswith("antigona-sandbox-")), "")
        event(
            "docker_sandbox_start",
            service="sandbox",
            correlation_id=correlation_id or None,
            task_id=task_id or None,
            image=self.image,
            container=container_id or None,
            command=shlex.join([str(c) for c in command]),
        )
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds + 5,  # +5s for container teardown
                check=False,
            )
            timed_out = False
            exit_code = proc.returncode
            stdout = proc.stdout
            stderr = proc.stderr
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            exit_code = 124  # conventional timeout exit code
            if isinstance(exc.stdout, bytes):
                stdout = exc.stdout.decode(errors="replace")
            else:
                stdout = exc.stdout or ""
            if isinstance(exc.stderr, bytes):
                stderr = exc.stderr.decode(errors="replace")
            else:
                stderr = exc.stderr or ""
            # best-effort cleanup of a leftover container on timeout
            if container_id:
                try:
                    subprocess.run(
                        [self.docker_binary, "rm", "-f", container_id],
                        capture_output=True,
                        timeout=10,
                        check=False,
                    )
                except Exception:
                    pass

        event(
            "docker_sandbox_finish",
            service="sandbox",
            correlation_id=correlation_id or None,
            task_id=task_id or None,
            container=container_id or None,
            exit_code=exit_code,
            timed_out=timed_out,
            stdout_tail=stdout[-500:] if stdout else "",
            stderr_tail=stderr[-500:] if stderr else "",
        )
        # `docker run` exited 125 AND docker's stderr says the image is missing /
        # its pull was refused: that is NOT the command's own failure, so it must
        # not be handed back as if it were the answer.  Narrow by construction
        # (125 + a specific marker) — every other docker failure keeps its real
        # exit code and output below.
        if image_missing_signal(exit_code, stderr):
            event(
                "docker_sandbox_image_missing",
                service="sandbox",
                correlation_id=correlation_id or None,
                task_id=task_id or None,
                image=self.image,
                status="blocked",
                exit_code=exit_code,
                reason=(
                    "docker run exit 125 with an image-missing/refused-pull stderr; "
                    "the socket proxy refuses pulls by design"
                ),
            )
            raise DockerSandboxUnavailableError(image_missing_message(self.image))
        return DockerSandboxResult(
            command=tuple(str(c) for c in command),
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            container_id=container_id,
            timed_out=timed_out,
        )


def shutil_which(binary: str) -> str | None:
    import shutil

    return shutil.which(binary)
