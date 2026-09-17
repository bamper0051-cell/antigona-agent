"""Sandbox launch profiles for tool containers (P0.4).

Second kernel-isolation layer (gVisor / ``runsc``) on top of the existing
fail-closed Docker profile, with a configurable fallback to ``runc``.
"""

from __future__ import annotations

from .microvm import (
    MicroVMExecResult,
    MicroVMProfile,
    MicroVMRunner,
    MicroVMUnavailableError,
    microvm_available,
)
from .runner import (
    ALLOW_RUNC_FALLBACK_ENV,
    DEFAULT_RUNTIME,
    GVISOR_RUNTIME,
    ISOLATION_DEGRADED,
    ISOLATION_GVISOR,
    ISOLATION_REFUSED,
    ISOLATION_RUNC,
    IsolationStatus,
    RuntimeProbe,
    SandboxIsolationError,
    SandboxProfile,
    SandboxRunner,
    build_run_argv,
    docker_available,
    docker_runtimes,
    ensure_runtime_available,
    fallback_allowed,
    probe_docker_runtimes,
    probe_isolation,
    resolve_runtime,
    runtime_registered,
    select_runtime,
    write_isolation_state,
)

__all__ = [
    "ALLOW_RUNC_FALLBACK_ENV",
    "DEFAULT_RUNTIME",
    "GVISOR_RUNTIME",
    "ISOLATION_DEGRADED",
    "ISOLATION_GVISOR",
    "ISOLATION_REFUSED",
    "ISOLATION_RUNC",
    "IsolationStatus",
    "MicroVMExecResult",
    "MicroVMProfile",
    "MicroVMRunner",
    "MicroVMUnavailableError",
    "RuntimeProbe",
    "SandboxIsolationError",
    "SandboxProfile",
    "SandboxRunner",
    "build_run_argv",
    "docker_available",
    "docker_runtimes",
    "ensure_runtime_available",
    "fallback_allowed",
    "microvm_available",
    "probe_docker_runtimes",
    "probe_isolation",
    "resolve_runtime",
    "runtime_registered",
    "select_runtime",
    "write_isolation_state",
]

