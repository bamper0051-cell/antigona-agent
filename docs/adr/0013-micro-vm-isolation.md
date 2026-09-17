# ADR-0013: Micro-VM (Firecracker / E2B) isolation for high-risk tools

- **Date:** 2026-07-26
- **Status:** Accepted
- **Full text:** [`docs/adr/0013-micro-vm-isolation.md`](0013-micro-vm-isolation.md)

## Context

In Antigona (P0.4), sandbox isolation relies on a fail-closed Docker profile with gVisor (`runsc`).
However, high-risk tools—specifically `WorkspaceShellTool` executing arbitrary agent commands—ran directly on the host using `subprocess.run(cwd=workspace)`.
To prevent potential host compromise or container escape, high-risk commands require hardware-enforced micro-VM boundaries (Firecracker or E2B).

## Decision

1. **Micro-VM Runner (`MicroVMRunner`)**:
   - Provide `MicroVMRunner` supporting Firecracker (REST API over unix socket) and E2B SDK as the highest tier of tool isolation.
   - Configure via `sandbox_runtime: "docker" | "firecracker" | "e2b"`.

2. **Fail-Closed Availability Probe**:
   - When `sandbox_runtime` is set to `firecracker` or `e2b`, the runtime MUST be available on the host (binary + `/dev/kvm` for Firecracker, or valid API key + SDK for E2B).
   - If unavailable, execution of high-risk tools is **BLOCKED** with `MicroVMUnavailableError` and a loud warning. **No silent fallback to host execution is permitted.**

3. **High-Risk Shell Routing**:
   - `WorkspaceShellTool` classifies commands into low-risk (P0 allowlist binaries without path separators) and high-risk (arbitrary execution or non-allowlist binaries).
   - Low-risk commands continue to run on host within the workspace directory.
   - High-risk commands are routed to `MicroVMRunner` when configured.

4. **Security Invariants**:
   - **Never `docker.sock`**: `docker.sock` is never exposed to or mounted into the guest VM. VM lifecycle management occurs strictly host-side.
   - **Egress Proxy Only**: VM guest networking is configured strictly to route through `EgressProxy` (P4.1), preserving deny-by-default egress filtering.
   - **Untrusted Output**: Execution output from micro-VMs is marked as `untrusted=True`.

## Consequences

- High-risk tool execution is isolated behind a strong VM boundary.
- Zero risk of unisolated host execution when `sandbox_runtime` is configured for micro-VM.
- Full compliance with clean-room requirements and independent verification rules.

