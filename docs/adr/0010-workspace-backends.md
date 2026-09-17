# ADR-0010: Workspace execution backends

- **Date:** 2026-07-26
- **Status:** Accepted
- **Deciders:** Architecture Board
- **References:** ADR-0001 (P0 boundary), ADR-0003 (P1 boundary), ADR-0004 (Verifier boundary), ADR-0009 (Subagents)

## Context

The ROADMAP P3 asks for *execution backends (SSH / Modal / Daytona)* via a single workspace abstraction (`BaseWorkspace`). Before P3.2, `WorkerAgentCore` instantiated `WorkspaceGuard` + `WorkspaceFileTools` + `WorkspaceShellTool` directly over `workspace.root_path` and performed operations directly on the local filesystem. For remote backends (SSH, Modal, Daytona), the agent continued to hit local tools directly, violating the requirement of backend swappability "without modifying agent code".

P3.2 consolidates the `BaseWorkspace` abstraction, adds `DaytonaWorkspace`, refactors `WorkerAgentCore` to delegate tool execution exclusively through `self.workspace.write_file/read_file/execute_command`, introduces honest `is_connected` probes and `WorkspaceConnectionError`, and separates mock vs. real adapters without requiring network or external SDKs during tests.

## Decision

### 1. `BaseWorkspace` is the sole execution surface for the agent.

`WorkerAgentCore` does not instantiate or invoke local filesystem tools directly. In `_tool_write_text`, `_tool_read_text`, and `_tool_shell`, it calls `self.workspace.write_file`, `self.workspace.read_file`, and `self.workspace.execute_command`. Local path-traversal guarding (`WorkspaceGuard`) is preserved inside `LocalWorkspace` and `DockerWorkspace`.

### 2. Dual Mock / Real adapter architecture for remote backends.

Every remote backend (`docker`, `ssh`, `modal`, `daytona`) has:
- A **Mock adapter** (`DockerWorkspace`, `SSHWorkspace`, `ModalWorkspace`, `DaytonaWorkspace`): performs operations safely on local storage / marked output (`[mock ...]` stdout) without network access or third-party SDK dependencies.
- A **Real adapter** (`DockerWorkspaceReal`, `SSHWorkspaceReal`, `ModalWorkspaceReal`, `DaytonaWorkspaceReal`): uses lazy SDK imports (`paramiko`, `modal`, `daytona_sdk`, `docker`). If credentials or SDKs are missing, calling `connect()` raises `WorkspaceConnectionError` and `is_connected` returns `False`.

### 3. Backend configuration without code modification.

`WorkspaceFactory.create_workspace(backend=..., workspace_dir=..., config=..., task_id=...)` selects the backend class using `Settings` / environment variables (`ANTIGONA_WORKSPACE_BACKEND`, `ANTIGONA_WORKSPACE_MOCK`, `ANTIGONA_SSH_*`, `ANTIGONA_MODAL_*`, `ANTIGONA_DAYTONA_*`). Custom backends register dynamically via `WorkspaceFactory.register(name, cls)`.

### 4. Per-task workspace isolation and subagent inheritance.

When `task_id` is supplied to `WorkspaceFactory.create_workspace`, the workspace root is scoped to a task subdirectory (`<workspace>/<task_id>`). Calling `ws.cleanup()` cleans up the task subdirectory or remote sandbox. Subagents (P3.1) inherit the parent flow's workspace backend and root path.

### 5. Backward compatibility and fallback.

`Orchestrator.__init__` accepts an optional `workspace: BaseWorkspace` parameter while preserving `tool` and `shell_tool` for backward compatibility with existing tests.

## Consequences

- Full decoupling of agent core execution from local filesystem details.
- Clean CI test execution without network access or external credentials.
- Safe escalation and error handling via `WorkspaceConnectionError` when remote connections fail.
