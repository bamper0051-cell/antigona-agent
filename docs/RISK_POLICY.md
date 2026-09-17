# Antigona Risk & Human-in-the-loop (HITL) Approval Policy

## 1. Overview

Antigona enforces security policy boundaries for agent operations via a Human-in-the-Loop (HITL) approval framework. Actions involving potential side effects, network access, or system modification require explicit user or administrator approval before execution.

## 2. Risk Classification Levels

Antigona classifies every requested tool action into one of three risk levels:

| Risk Level | Description | Example Operations | Default Policy |
| :--- | :--- | :--- | :--- |
| **`LOW`** | Safe, read-only operations restricted within workspace boundaries. | `workspace.read_text` (normal workspace file), workspace status checks. | Auto-Approved |
| **`MEDIUM`** | File modifications inside workspace boundaries or non-destructive workspace operations. | `workspace.write_text` (normal workspace target), safe local code generation. | Requires Approval |
| **`HIGH`** | Destructive commands, system setting alterations, path escapes, or external network access. | `sandbox.shell` (`rm -rf`, `sudo`, `chmod`, exfiltration), `web.fetch` (HTTP), paths with `..` or system directories (`/etc`, `/usr`). | Requires Approval |

## 3. Heuristic Risk Classifier Rules

The lightweight risk classifier (`evaluate_risk`) evaluates tools deterministically without requiring external API dependencies:

1. **Network Access**: Any invocation of `web.fetch`, `web_fetch`, or `network.http` is classified as **`HIGH`** risk.
2. **Shell Execution**: Shell execution (`sandbox.shell`) is inspected for:
   - Destructive or administrative commands (`rm`, `sudo`, `su`, `chmod`, `chown`, `dd`, `mkfs`, etc.) &rarr; **`HIGH`** risk.
   - Network tools (`curl`, `wget`, `nc`, `ssh`, `scp`, etc.) &rarr; **`HIGH`** risk.
   - External/Sensitive paths (`..`, `/etc`, `/usr`, `/root`, `.env`, `.ssh`) &rarr; **`HIGH`** risk.
   - Safe shell commands &rarr; **`MEDIUM`** risk.
3. **Workspace File Modification**: Writing files (`workspace.write_text`) inside workspace bounds &rarr; **`MEDIUM`** risk. Writes attempting path escape or accessing sensitive files &rarr; **`HIGH`** risk.
4. **Workspace File Reading**: Reading files (`workspace.read_text`) inside workspace bounds &rarr; **`LOW`** risk. Reading sensitive paths (`/etc/passwd`, `.env`, `.ssh`) &rarr; **`HIGH`** risk.

## 4. Confirmation Policies

Antigona supports dynamic policy configuration via `set_confirmation_policy()`:

- **`ALWAYS`** (Default): Requires approval for both `MEDIUM` and `HIGH` risk actions.
- **`HIGH_ONLY`**: Requires approval for `HIGH` risk actions; auto-approves `MEDIUM` and `LOW` risk actions.
- **`NEVER`**: Auto-approves all actions (used for trusted automation or headless test environments).

## 5. Decision Lifecycle & Timeout Auto-Reject

1. **Request & Transition**: When a tool action requires approval, the flow state machine transitions the task to **`WAITING_APPROVAL`** and records an `Approval` entry with state `PENDING`.
2. **Channel & Gateway Exposure**: Pending approvals are queryable via `GET /flows/{id}` and pushed via WebSocket to connected clients and the Telegram Bot (which displays inline `✅ Approve` / `❌ Reject` buttons).
3. **User Decision**:
   - **Approve**: User sends decision via `POST /approvals/{id}/decision` (`approve: true`). Task transitions to `TOOL_EXECUTING` and executes the tool.
   - **Reject**: User sends decision (`approve: false`). Task transitions to **`POLICY_DENIED`** and execution halts without running the tool.
4. **Timeout**: Approvals remaining `PENDING` longer than `timeout_seconds` (default: 60.0s) are automatically auto-rejected by the system (`decided_by: system_timeout`), transitioning the task to **`POLICY_DENIED`**.

## 6. Durable State & Crash Recovery

All approval states (`PENDING`, `APPROVED`, `DENIED`) and flow transitions are stored durably in the SQLite/PostgreSQL database. If a Worker restarts while a flow is in `WAITING_APPROVAL`, state is preserved cleanly without double execution or corrupted state.
