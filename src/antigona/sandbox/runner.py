from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from ..observability import event

LOGGER = logging.getLogger("antigona")

#: gVisor's OCI runtime name; the strong (kernel-isolating) option.
GVISOR_RUNTIME = "runsc"
#: Plain Docker runtime; the WEAKER option (shares the host kernel).
DEFAULT_RUNTIME = "runc"

#: Isolation levels reported to operators and health probes.  The value MUST
#: reflect the runtime that is actually launchable right now (a live probe),
#: never a configuration flag:
#:   * ``gvisor``   — gVisor ``runsc`` is registered; every sandboxed command
#:                    runs against a separate user-space kernel;
#:   * ``runc``     — the operator EXPLICITLY chose the weaker runtime AND the
#:                    escape hatch is on (loudly logged, still visible);
#:   * ``degraded`` — auto-selection wanted gVisor but it is unavailable and the
#:                    escape hatch is on: a WEAKENED level (loudly logged);
#:   * ``refused``  — gVisor is unavailable and the escape hatch is off; command
#:                    execution is REFUSED (fail-closed, never ``runc``).
ISOLATION_GVISOR = "gvisor"
ISOLATION_RUNC = "runc"
ISOLATION_DEGRADED = "degraded"
ISOLATION_REFUSED = "refused"

#: Escape hatch.  DEFAULT OFF (absent == off): a silent host-kernel downgrade is
#: forbidden, so ``runc`` is only ever used when this is EXPLICITLY enabled, and
#: every such use is logged loudly and reported as ``runc``/``degraded``.
ALLOW_RUNC_FALLBACK_ENV = "ANTIGONA_SANDBOX_ALLOW_RUNC_FALLBACK"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def fallback_allowed() -> bool:
    """True only when the operator EXPLICITLY opted into the weaker runtime."""
    return os.getenv(ALLOW_RUNC_FALLBACK_ENV, "").strip().lower() in _TRUTHY


class SandboxIsolationError(RuntimeError):
    """No acceptable kernel-isolating runtime is available (fail-closed).

    Raised INSTEAD OF silently substituting the weaker ``runc`` runtime.  The
    message always names the missing runtime and the concrete remedy.
    """

#: subprocess.run-compatible callable, injected in tests.
RunnerFn = Callable[..., "subprocess.CompletedProcess[str]"]


@dataclass(frozen=True)
class RuntimeProbe:
    """Result of probing the Docker daemon for its registered OCI runtimes.

    ``error`` is kept separate from ``available`` so the refusal path can name
    the REAL problem: a missing ``runsc`` registration is actionable, a dead
    daemon is a different remedy.
    """

    available: frozenset[str]
    error: str | None = None

    @property
    def reachable(self) -> bool:
        return self.error is None and bool(self.available)


def probe_docker_runtimes(runner: RunnerFn = subprocess.run) -> RuntimeProbe:
    """Probe the daemon once, preserving WHY an empty set came back."""
    try:
        proc = runner(
            ["docker", "info", "--format", "{{json .Runtimes}}"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except FileNotFoundError as exc:
        return RuntimeProbe(frozenset(), f"docker CLI not found ({exc})")
    except (OSError, subprocess.SubprocessError) as exc:
        return RuntimeProbe(frozenset(), f"docker daemon unreachable ({exc})")
    if getattr(proc, "returncode", 1) != 0:
        err = ((getattr(proc, "stderr", "") or "").strip() or "non-zero exit")
        return RuntimeProbe(frozenset(), f"docker daemon unreachable ({err[:200]})")
    try:
        data = json.loads((getattr(proc, "stdout", "") or "").strip() or "{}")
    except (TypeError, ValueError) as exc:
        return RuntimeProbe(frozenset(), f"unparsable docker runtime list ({exc})")
    if not isinstance(data, dict):
        return RuntimeProbe(frozenset(), "unexpected docker runtime list shape")
    return RuntimeProbe(frozenset(str(key) for key in data))


def docker_runtimes(runner: RunnerFn = subprocess.run) -> frozenset[str]:
    """Return the OCI runtime names Docker has registered (empty on any error)."""
    return probe_docker_runtimes(runner).available


def runtime_registered(name: str, runner: RunnerFn = subprocess.run) -> bool:
    """True when *name* is a runtime Docker can launch right now."""
    return name in docker_runtimes(runner)


def docker_available(runner: RunnerFn = subprocess.run) -> bool:
    """True when a Docker daemon is reachable (any runtime present)."""
    return bool(docker_runtimes(runner))


def _unavailable_reason(probe: RuntimeProbe) -> str:
    if not probe.available:
        return probe.error or "no OCI runtime is registered with the Docker daemon"
    return (
        f"OCI runtime {GVISOR_RUNTIME!r} is not registered with the Docker daemon "
        f"(available={sorted(probe.available)})"
    )


def _refusal_message(preferred: str, probe: RuntimeProbe) -> str:
    return (
        f"Sandbox isolation REFUSED: {_unavailable_reason(probe)}. "
        f"Executing on the weaker {DEFAULT_RUNTIME!r} runtime (shared host kernel) is "
        f"NOT permitted by default. Remedy: install gVisor and register it as "
        f"{GVISOR_RUNTIME!r} in /etc/docker/daemon.json under "
        f'runtimes.{GVISOR_RUNTIME}.path (e.g. "/usr/local/bin/{GVISOR_RUNTIME}"), '
        f"then restart docker and confirm "
        f"`docker info --format '{{{{json .Runtimes}}}}'` lists {GVISOR_RUNTIME!r}. "
        f"To DELIBERATELY accept the weaker host-kernel runtime, set "
        f"{ALLOW_RUNC_FALLBACK_ENV}=1 (logged loudly, reported as DEGRADED)."
    )


def _explicit_runc_refusal_message() -> str:
    return (
        f"Sandbox isolation REFUSED: sandbox_runtime is explicitly configured as "
        f"{DEFAULT_RUNTIME!r}, which disables gVisor kernel isolation (the container "
        f"would share the host kernel). Refusing by default. Remedy: set "
        f"sandbox_runtime=auto (or {GVISOR_RUNTIME!r}) to use gVisor, or set "
        f"{ALLOW_RUNC_FALLBACK_ENV}=1 to DELIBERATELY accept the weaker runtime "
        f"(logged loudly, reported as DEGRADED)."
    )


def select_runtime(
    preferred: str,
    available: frozenset[str],
    *,
    probe_error: str | None = None,
    log: Callable[..., None] = event,
) -> str:
    """Resolve the configured preference against what Docker actually offers.

    FAIL-CLOSED: gVisor is REQUIRED.  When ``runsc`` is registered the strong
    runtime is selected.  When it is NOT, execution is refused with
    :class:`SandboxIsolationError` naming the missing runtime and the remedy —
    the weaker ``runc`` runtime is never substituted silently.  ``runc`` is
    reachable only through the explicit escape hatch
    (``ANTIGONA_SANDBOX_ALLOW_RUNC_FALLBACK=1``), which is logged loudly and
    reported as a non-``gvisor`` isolation level.
    """
    choice = (preferred or "auto").strip().lower()
    probe = RuntimeProbe(frozenset(available), probe_error)

    # No Docker at all: nothing can be launched — refuse.
    if not available:
        message = _refusal_message(choice, probe)
        LOGGER.error("%s", message)
        log(
            "sandbox_isolation_refused",
            service="sandbox",
            correlation_id=None,
            status=ISOLATION_REFUSED,
            requested=choice,
            selected=None,
            reason=_unavailable_reason(probe),
            available=[],
        )
        raise SandboxIsolationError(message)

    if choice not in {"auto", GVISOR_RUNTIME, DEFAULT_RUNTIME}:
        LOGGER.warning(
            "unknown sandbox runtime preference %r — resolving via auto-detection",
            preferred,
        )
        choice = "auto"

    if choice in {"auto", GVISOR_RUNTIME}:
        if GVISOR_RUNTIME in available:
            log(
                "sandbox_runtime_selected",
                service="sandbox",
                correlation_id=None,
                status=ISOLATION_GVISOR,
                requested=choice,
                selected=GVISOR_RUNTIME,
                reason="gvisor available",
            )
            return GVISOR_RUNTIME
        if not fallback_allowed():
            message = _refusal_message(choice, probe)
            LOGGER.error("%s", message)
            log(
                "sandbox_isolation_refused",
                service="sandbox",
                correlation_id=None,
                status=ISOLATION_REFUSED,
                requested=choice,
                selected=None,
                reason=_unavailable_reason(probe),
                available=sorted(available),
            )
            raise SandboxIsolationError(message)
        LOGGER.warning(
            "gVisor runtime %r is NOT registered in Docker — DEGRADED isolation: "
            "using %r (shared host kernel) because %s=1. requested=%s available=%s",
            GVISOR_RUNTIME,
            DEFAULT_RUNTIME,
            ALLOW_RUNC_FALLBACK_ENV,
            choice,
            sorted(available),
        )
        log(
            "sandbox_runtime_degraded",
            service="sandbox",
            correlation_id=None,
            status=ISOLATION_DEGRADED,
            requested=choice,
            selected=DEFAULT_RUNTIME,
            reason=_unavailable_reason(probe),
            available=sorted(available),
        )
        return DEFAULT_RUNTIME

    # Explicit runc preference.
    if not fallback_allowed():
        message = _explicit_runc_refusal_message()
        LOGGER.error("%s", message)
        log(
            "sandbox_isolation_refused",
            service="sandbox",
            correlation_id=None,
            status=ISOLATION_REFUSED,
            requested=choice,
            selected=None,
            reason="explicit runc preference without the escape hatch",
            available=sorted(available),
        )
        raise SandboxIsolationError(message)
    LOGGER.warning(
        "sandbox runtime EXPLICITLY configured as %r — gVisor kernel isolation is "
        "DISABLED (DEGRADED, shared host kernel); %s=1 acknowledges it.",
        DEFAULT_RUNTIME,
        ALLOW_RUNC_FALLBACK_ENV,
    )
    log(
        "sandbox_runtime_selected",
        service="sandbox",
        correlation_id=None,
        status=ISOLATION_RUNC,
        requested=choice,
        selected=DEFAULT_RUNTIME,
        reason="explicit runc preference with the escape hatch enabled",
    )
    return DEFAULT_RUNTIME


def resolve_runtime(
    preferred: str = "auto",
    *,
    runner: RunnerFn = subprocess.run,
    log: Callable[..., None] = event,
) -> str:
    """Query Docker and resolve *preferred* to a concrete runtime name.

    Fail-closed: raises :class:`SandboxIsolationError` when gVisor is not
    available and the escape hatch is off — never a silent ``runc`` substitution.
    """
    probe = probe_docker_runtimes(runner)
    return select_runtime(
        preferred, probe.available, probe_error=probe.error, log=log
    )


@dataclass(frozen=True)
class IsolationStatus:
    """The isolation a sandboxed command would ACTUALLY get, probed live."""

    level: str
    runtime: str | None
    available: tuple[str, ...]
    allow_fallback: bool
    detail: str = ""

    @property
    def kernel_isolated(self) -> bool:
        return self.level == ISOLATION_GVISOR

    def as_dict(self) -> dict[str, object]:
        return {
            "level": self.level,
            "runtime": self.runtime,
            "kernel_isolated": self.kernel_isolated,
            "available_runtimes": list(self.available),
            "allow_runc_fallback": self.allow_fallback,
            "detail": self.detail,
        }


def probe_isolation(
    preferred: str = "auto",
    *,
    runner: RunnerFn = subprocess.run,
) -> IsolationStatus:
    """Probe (LIVE) the isolation level a sandboxed command would get.

    Reflective, not configured: the value comes from a fresh ``docker info``
    probe, so an external check reading it detects a downgrade even when the
    configuration (``sandbox_runtime``) never changed.
    """
    probe = probe_docker_runtimes(runner)
    available = probe.available
    allow = fallback_allowed()
    ordered = tuple(sorted(available))
    if not available:
        return IsolationStatus(
            ISOLATION_REFUSED, None, ordered, allow, _unavailable_reason(probe)
        )
    choice = (preferred or "auto").strip().lower()
    if choice not in {"auto", GVISOR_RUNTIME, DEFAULT_RUNTIME}:
        choice = "auto"
    if GVISOR_RUNTIME in available and choice in {"auto", GVISOR_RUNTIME}:
        return IsolationStatus(
            ISOLATION_GVISOR,
            GVISOR_RUNTIME,
            ordered,
            allow,
            "gVisor runsc registered — separate user-space kernel active",
        )
    if not allow:
        detail = (
            _explicit_runc_refusal_message()
            if choice == DEFAULT_RUNTIME
            else _refusal_message(choice, probe)
        )
        return IsolationStatus(ISOLATION_REFUSED, None, ordered, allow, detail)
    if choice == DEFAULT_RUNTIME:
        return IsolationStatus(
            ISOLATION_RUNC,
            DEFAULT_RUNTIME,
            ordered,
            allow,
            "runc explicitly configured with the escape hatch enabled — DEGRADED",
        )
    return IsolationStatus(
        ISOLATION_DEGRADED,
        DEFAULT_RUNTIME,
        ordered,
        allow,
        "gVisor unavailable and the escape hatch is enabled — weaker host kernel "
        f"in use: {_unavailable_reason(probe)}",
    )


def ensure_runtime_available(
    runtime: str,
    *,
    runner: RunnerFn = subprocess.run,
) -> None:
    """Pre-execution guard: refuse when *runtime* is not currently acceptable.

    The runtime is re-verified against Docker on EVERY call so a registration
    that disappeared after startup (daemon restart without the runtime, an
    upgrade that dropped ``runsc``, a deleted binary) cannot silently downgrade
    execution to the host kernel.  Raises :class:`SandboxIsolationError`.
    """
    if runtime == GVISOR_RUNTIME:
        probe = probe_docker_runtimes(runner)
        if GVISOR_RUNTIME in probe.available:
            return
        raise SandboxIsolationError(_refusal_message("runsc", probe))
    if runtime == DEFAULT_RUNTIME:
        if not fallback_allowed():
            raise SandboxIsolationError(_explicit_runc_refusal_message())
        return
    raise SandboxIsolationError(
        f"Sandbox isolation REFUSED: runtime {runtime!r} is not a verified "
        f"kernel-isolating runtime; no sandboxed command will run without gVisor "
        f"({GVISOR_RUNTIME!r})."
    )


def isolation_state_path() -> Path:
    """Canonical runtime state file for the probed isolation level."""
    from ..core import paths

    return paths.isolation_state_file()


def write_isolation_state(
    status: IsolationStatus,
    *,
    preferred: str = "auto",
) -> Path | None:
    """Best-effort write of the live isolation level into the runtime state root."""
    path = isolation_state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = dict(status.as_dict())
        payload["preferred"] = preferred
        payload["ts"] = time.time()
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        tmp.replace(path)
        return path
    except OSError:
        return None


def resolve_workspace_uid_gid(workspace: Path | str | None) -> tuple[int, int]:
    """Resolve the effective (uid, gid) that owns and has write access to the workspace.

    Under --cap-drop=ALL, root (0:0) has no CAP_DAC_OVERRIDE and cannot write
    to a non-root owned 0750 workspace. Running the container with the workspace
    directory owner's UID/GID ensures write access without relaxing isolation or
    adding capabilities.
    """
    if workspace is not None:
        try:
            p = Path(workspace).resolve()
            if p.exists():
                st = p.stat()
                if (hasattr(os, "getuid") and os.getuid() == 0 and st.st_uid != 0) or st.st_uid != 0:
                    return st.st_uid, st.st_gid
        except OSError:
            pass
    uid = os.getuid() if hasattr(os, "getuid") else 1000
    gid = os.getgid() if hasattr(os, "getgid") else 1000
    return uid, gid


@dataclass(frozen=True)
class SandboxProfile:
    """The complete fail-closed launch profile for a tool container.

    Every field maps to a hardening flag; the container gets no network, a
    read-only root, all capabilities dropped, a non-root UID/GID, CPU/RAM/PID
    ceilings, a ``noexec,nosuid`` tmpfs, and a single read-write ``0750``
    workspace mount. ``runtime`` layers gVisor on top when available.
    """

    workspace: Path
    image: str = "python:3.12-alpine"
    runtime: str = DEFAULT_RUNTIME
    memory: str = "128m"
    cpus: str = "0.5"
    pids_limit: int = 64
    tmpfs_size: str = "16m"
    stop_timeout: int = 30
    network: str = "none"
    read_only: bool = True
    caps_add: tuple[str, ...] = ()
    mount_target: str = "/workspace"
    workdir: str = "/workspace"
    uid: int = field(default_factory=lambda: os.getuid() if hasattr(os, "getuid") else 1000)
    gid: int = field(default_factory=lambda: os.getgid() if hasattr(os, "getgid") else 1000)

    def hardened_workspace(self) -> Path:
        """Resolve the workspace and enforce its ``0750`` mode, creating it."""
        resolved = self.workspace.resolve()
        resolved.mkdir(parents=True, exist_ok=True, mode=0o750)
        resolved.chmod(0o750)
        return resolved


def build_run_argv(
    profile: SandboxProfile,
    command: Sequence[str],
    *,
    name: str | None = None,
    interactive: bool = False,
    remove: bool = True,
    extra_flags: Sequence[str] = (),
) -> list[str]:
    """Build the ``docker run`` argv for a tool container.

    The security flags are non-optional and always emitted in a fixed order so
    the profile stays auditable. ``--runtime`` is always explicit so gVisor vs
    runc is never left to a daemon default. Never mounts ``docker.sock``.
    """
    workspace = profile.workspace.resolve()
    argv: list[str] = ["docker", "run"]
    if remove:
        argv.append("--rm")
    if interactive:
        argv.append("-i")
    if name:
        argv += ["--name", name]
    argv += [
        f"--runtime={profile.runtime}",
        f"--network={profile.network}",
    ]
    # Fail-closed default is a read-only root. Install-capable profiles
    # (package managers need to write) set read_only=False explicitly.
    if profile.read_only:
        argv.append("--read-only")
    argv += [
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        f"--user={profile.uid}:{profile.gid}",
        f"--memory={profile.memory}",
        f"--cpus={profile.cpus}",
        f"--pids-limit={profile.pids_limit}",
        f"--stop-timeout={profile.stop_timeout}",
        "--tmpfs",
        f"/tmp:rw,noexec,nosuid,size={profile.tmpfs_size}",
        "-v",
        f"{workspace}:{profile.mount_target}:rw",
        "-w",
        profile.workdir,
    ]
    # Install-capable profiles restore SETGID/SETUID so apt can drop
    # privileges to its "_apt" sandbox user (needs setgroups/setuid).
    # This is privilege DOWN-scoping, not escalation — the container still
    # starts as its configured user with all capabilities dropped.
    for cap in profile.caps_add:
        argv.append(f"--cap-add={cap}")
    argv += list(extra_flags)
    argv.append(profile.image)
    argv += list(command)
    return argv


@dataclass
class SandboxRunner:
    """High-level launcher tying runtime selection to the fail-closed profile."""

    profile: SandboxProfile

    @classmethod
    def create(
        cls,
        workspace: Path,
        *,
        image: str = "python:3.12-alpine",
        preferred_runtime: str = "auto",
        runner: RunnerFn = subprocess.run,
        log: Callable[..., None] = event,
        **profile_kwargs: object,
    ) -> SandboxRunner:
        """Resolve the runtime once and freeze a profile around it."""
        runtime = resolve_runtime(preferred_runtime, runner=runner, log=log)
        profile = SandboxProfile(
            workspace=workspace,
            image=image,
            runtime=runtime,
            **profile_kwargs,  # type: ignore[arg-type]
        )
        return cls(profile)

    @property
    def runtime(self) -> str:
        return self.profile.runtime

    def under_gvisor(self) -> bool:
        return self.profile.runtime == GVISOR_RUNTIME

    def argv(
        self,
        command: Sequence[str],
        *,
        name: str | None = None,
        interactive: bool = False,
    ) -> list[str]:
        return build_run_argv(
            self.profile, command, name=name, interactive=interactive
        )

    def run(
        self,
        command: Sequence[str],
        *,
        timeout: int = 30,
        stdin: str | None = None,
        name: str | None = None,
        runner: RunnerFn = subprocess.run,
    ) -> subprocess.CompletedProcess[str]:
        """Execute a tool container under the resolved runtime."""
        self.profile.hardened_workspace()
        argv = self.argv(command, name=name, interactive=stdin is not None)
        return runner(
            argv,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
