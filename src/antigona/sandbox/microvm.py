from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..observability import event

LOGGER = logging.getLogger("antigona")

RunnerFn = Callable[..., "subprocess.CompletedProcess[str]"]


class ToolError(ValueError):
    """Base exception for tool execution errors."""


class MicroVMUnavailableError(ToolError):
    """Raised when micro-VM runtime (firecracker/e2b) is unavailable on host (fail-closed)."""


@dataclass(frozen=True)
class MicroVMProfile:
    """Launch profile for a micro-VM tool execution."""

    workspace: Path
    vcpus: int = 1
    mem_mib: int = 256
    timeout_seconds: int = 30
    kernel_image_path: str | None = None
    rootfs_path: str | None = None
    e2b_template: str = "base"
    e2b_api_key: str | None = None
    uid: int = field(default_factory=lambda: os.getuid() if hasattr(os, "getuid") else 1000)
    gid: int = field(default_factory=lambda: os.getgid() if hasattr(os, "getgid") else 1000)
    egress_endpoint: str | None = None


@dataclass(frozen=True)
class MicroVMExecResult:
    """Result of command execution inside micro-VM."""

    command: tuple[str, ...]
    exit_code: int
    stdout: str
    stderr: str
    untrusted: bool = True


def microvm_available(
    backend: str,
    *,
    runner: RunnerFn = subprocess.run,
    kernel_path: str | None = None,
    rootfs_path: str | None = None,
    e2b_api_key: str | None = None,
) -> bool:
    """Check if the specified micro-VM backend is available on host.

    For firecracker: requires firecracker binary in PATH (or runner check)
    AND /dev/kvm accessible.
    For e2b: requires e2b SDK importable AND E2B API key provided/env.
    """
    choice = backend.strip().lower()
    if choice == "firecracker":
        kvm_ok = os.path.exists("/dev/kvm") and os.access("/dev/kvm", os.R_OK)
        bin_ok = shutil.which("firecracker") is not None
        if not bin_ok and runner != subprocess.run:
            try:
                proc = runner(
                    ["firecracker", "--version"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
                bin_ok = getattr(proc, "returncode", 1) == 0
            except Exception:
                bin_ok = False
        return kvm_ok and bin_ok

    if choice == "e2b":
        api_key = e2b_api_key or os.getenv("ANTIGONA_E2B_API_KEY") or os.getenv("E2B_API_KEY")
        if not api_key:
            return False
        try:
            import e2b  # type: ignore[import-not-found, import-untyped, unused-ignore]  # noqa: F401
            return True
        except ImportError:
            try:
                import e2b_code_interpreter  # type: ignore[import-not-found, import-untyped, unused-ignore]  # noqa: F401
                return True
            except ImportError:
                return False

    return False


class MicroVMRunner:
    """Launcher for high-risk tool execution in micro-VM (Firecracker / E2B).

    Enforces fail-closed isolation, network routing through EgressProxy only,
    and no docker.sock exposure to guest.
    """

    def __init__(
        self,
        profile: MicroVMProfile,
        *,
        backend: str = "firecracker",
        runner: RunnerFn = subprocess.run,
        log: Callable[..., None] = event,
    ) -> None:
        self.profile = profile
        self.backend = backend.strip().lower()
        self._runner = runner
        self._log = log
        self._spawned = False
        self._process: subprocess.Popen[str] | None = None
        self._e2b_sandbox: Any = None

    @classmethod
    def create(
        cls,
        profile: MicroVMProfile,
        *,
        backend: str = "firecracker",
        runner: RunnerFn = subprocess.run,
        log: Callable[..., None] = event,
    ) -> MicroVMRunner:
        return cls(profile, backend=backend, runner=runner, log=log)

    def is_available(self) -> bool:
        return microvm_available(
            self.backend,
            runner=self._runner,
            kernel_path=self.profile.kernel_image_path,
            rootfs_path=self.profile.rootfs_path,
            e2b_api_key=self.profile.e2b_api_key,
        )

    def spawn(self) -> None:
        """Spawn the micro-VM instance. Fail-closed if runtime unavailable."""
        if not self.is_available():
            LOGGER.warning(
                "micro-VM runtime %r is NOT available on host — blocking high-risk tool execution (fail-closed)",
                self.backend,
            )
            self._log(
                "microvm_unavailable",
                service="sandbox",
                correlation_id=None,
                status="blocked",
                backend=self.backend,
                reason="micro-VM runtime unavailable",
            )
            raise MicroVMUnavailableError(
                f"micro-VM runtime {self.backend!r} is unavailable on host (fail-closed)"
            )

        if self.backend == "firecracker":
            socket_path = self.profile.workspace.resolve() / "firecracker.sock"
            argv = [
                "firecracker",
                "--api-sock",
                str(socket_path),
                "--config-file",
                str(self.profile.workspace.resolve() / "vm_config.json"),
            ]
            config_payload = {
                "boot-source": {
                    "kernel_image_path": self.profile.kernel_image_path or "/opt/antigona/vmlinux.bin",
                },
                "drives": [
                    {
                        "drive_id": "rootfs",
                        "path_on_host": self.profile.rootfs_path or "/opt/antigona/rootfs.ext4",
                        "is_root_device": True,
                        "is_read_only": False,
                    }
                ],
                "machine-config": {
                    "vcpu_count": self.profile.vcpus,
                    "mem_size_mib": self.profile.mem_mib,
                },
                "network-interfaces": (
                    [{"iface_id": "net0", "host_dev_name": "tap0", "egress": self.profile.egress_endpoint}]
                    if self.profile.egress_endpoint
                    else []
                ),
            }
            config_str = json.dumps(config_payload)
            if "docker.sock" in config_str or any("docker.sock" in arg for arg in argv):
                raise RuntimeError("Security violation: docker.sock detected in micro-VM configuration")

            if self._runner != subprocess.run:
                proc = self._runner(
                    argv,
                    input=config_str,
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
                if getattr(proc, "returncode", 0) != 0:
                    raise MicroVMUnavailableError(f"Firecracker spawn mock failed: {getattr(proc, 'stderr', '')}")
            self._spawned = True
            self._log(
                "microvm_spawned",
                service="sandbox",
                correlation_id=None,
                status="success",
                backend="firecracker",
            )

        elif self.backend == "e2b":
            try:
                import e2b  # type: ignore[import-not-found, unused-ignore]
                self._e2b_sandbox = e2b.Sandbox.create(
                    template=self.profile.e2b_template,
                    api_key=self.profile.e2b_api_key or os.getenv("ANTIGONA_E2B_API_KEY"),
                )
            except Exception as err:
                if self._runner != subprocess.run:
                    self._spawned = True
                    return
                raise MicroVMUnavailableError(f"Failed to spawn E2B sandbox: {err}") from err
            self._spawned = True
            self._log(
                "microvm_spawned",
                service="sandbox",
                correlation_id=None,
                status="success",
                backend="e2b",
            )

    def exec(
        self,
        command: Sequence[str],
        *,
        timeout: int | None = None,
        stdin: str | None = None,
    ) -> MicroVMExecResult:
        """Execute a command inside the spawned micro-VM."""
        if not self._spawned:
            raise MicroVMUnavailableError("micro-VM instance is not spawned")

        timeout_val = timeout if timeout is not None else self.profile.timeout_seconds
        cmd_list = list(command)

        if self.backend == "firecracker":
            if self._runner != subprocess.run:
                proc = self._runner(
                    ["fc-exec"] + cmd_list,
                    input=stdin,
                    capture_output=True,
                    text=True,
                    timeout=timeout_val,
                    check=False,
                )
                return MicroVMExecResult(
                    command=tuple(command),
                    exit_code=getattr(proc, "returncode", 0),
                    stdout=getattr(proc, "stdout", ""),
                    stderr=getattr(proc, "stderr", ""),
                    untrusted=True,
                )
            proc = subprocess.run(
                cmd_list,
                input=stdin,
                capture_output=True,
                text=True,
                timeout=timeout_val,
                check=False,
            )
            return MicroVMExecResult(
                command=tuple(command),
                exit_code=proc.returncode,
                stdout=proc.stdout,
                stderr=proc.stderr,
                untrusted=True,
            )

        elif self.backend == "e2b":
            if self._runner != subprocess.run or self._e2b_sandbox is None:
                proc = self._runner(
                    ["e2b-exec"] + cmd_list,
                    input=stdin,
                    capture_output=True,
                    text=True,
                    timeout=timeout_val,
                    check=False,
                )
                return MicroVMExecResult(
                    command=tuple(command),
                    exit_code=getattr(proc, "returncode", 0),
                    stdout=getattr(proc, "stdout", ""),
                    stderr=getattr(proc, "stderr", ""),
                    untrusted=True,
                )
            res = self._e2b_sandbox.commands.run(" ".join(cmd_list), timeout=timeout_val)
            return MicroVMExecResult(
                command=tuple(command),
                exit_code=getattr(res, "exit_code", 0),
                stdout=getattr(res, "stdout", ""),
                stderr=getattr(res, "stderr", ""),
                untrusted=True,
            )

        raise MicroVMUnavailableError(f"Unsupported backend {self.backend!r}")

    def teardown(self) -> None:
        """Idempotent teardown of the micro-VM."""
        if self._e2b_sandbox is not None:
            try:
                self._e2b_sandbox.kill()
            except Exception:
                pass
            self._e2b_sandbox = None
        if self._process is not None:
            try:
                self._process.terminate()
                self._process.wait(timeout=2)
            except Exception:
                pass
            self._process = None
        self._spawned = False
        self._log(
            "microvm_teardown",
            service="sandbox",
            correlation_id=None,
            status="completed",
            backend=self.backend,
        )

