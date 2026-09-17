"""Pure status and outcome formatting helpers for CLI UI presentation layer.

Renders FlowView, ApprovalView, and TerminalOutcome objects into presentation
ChatMessage instances without contacting DB, network, or external runtime.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from antigona.cli_ui.models import (
    ChatMessage,
    ChatMessageRole,
    TerminalOutcome,
    TerminalOutcomeStatus,
)


def render_flow_status(flow_view: Any) -> ChatMessage:
    """Render a FlowView or GatewayFlowView into a ChatMessage."""
    flow_id = getattr(flow_view, "flow_id", getattr(flow_view, "id", "unknown"))
    status = str(getattr(flow_view, "status", "UNKNOWN"))
    title = getattr(flow_view, "title", getattr(flow_view, "goal", ""))
    content = f"Flow [{flow_id}] status: {status}"
    if title:
        content += f" | Goal/Title: {title}"
    return ChatMessage(
        role=ChatMessageRole.INFO,
        content=content,
        metadata={"flow_id": flow_id, "status": status},
    )


def render_approval_list(approvals: Any) -> ChatMessage:
    """Render an ApprovalListView or sequence of ApprovalView objects into a ChatMessage."""
    if approvals is None:
        return ChatMessage(
            role=ChatMessageRole.INFO,
            content="No pending approvals.",
        )

    if isinstance(approvals, dict):
        return ChatMessage(
            role=ChatMessageRole.ERROR,
            content="Gateway response was malformed.",
        )

    if hasattr(approvals, "items"):
        items = approvals.items
    elif isinstance(approvals, (list, tuple)):
        items = approvals
    else:
        return ChatMessage(
            role=ChatMessageRole.ERROR,
            content="Gateway response was malformed.",
        )

    if items is None or not isinstance(items, (list, tuple)):
        return ChatMessage(
            role=ChatMessageRole.ERROR,
            content="Gateway response was malformed.",
        )

    if len(items) == 0:
        return ChatMessage(
            role=ChatMessageRole.INFO,
            content="No pending approvals.",
        )

    lines = ["Pending Approvals:"]
    for app in items:
        if isinstance(app, dict):
            return ChatMessage(
                role=ChatMessageRole.ERROR,
                content="Gateway response was malformed.",
            )
        app_id = getattr(app, "approval_id", getattr(app, "id", None))
        if app_id is None:
            return ChatMessage(
                role=ChatMessageRole.ERROR,
                content="Gateway response was malformed.",
            )
        tool_name = getattr(app, "tool_name", getattr(app, "action", ""))
        reason = getattr(app, "reason", getattr(app, "target", ""))
        decision = getattr(app, "decision", getattr(app, "status", ""))
        lines.append(f"  • ID: {app_id} | Tool: {tool_name} | Reason: {reason} | Decision: {decision}")

    return ChatMessage(
        role=ChatMessageRole.INFO,
        content="\n".join(lines),
        metadata={"count": len(items)},
    )


def render_flow_list(flows: Sequence[Any]) -> ChatMessage:
    """Render a sequence of FlowView objects into a ChatMessage."""
    if not flows:
        return ChatMessage(
            role=ChatMessageRole.INFO,
            content="No flows found.",
        )

    lines = ["Active Flows:"]
    for flow in flows:
        flow_id = getattr(flow, "flow_id", getattr(flow, "id", "unknown"))
        status = getattr(flow, "status", "UNKNOWN")
        title = getattr(flow, "title", getattr(flow, "goal", ""))
        lines.append(f"  • Flow [{flow_id}] status: {status} | Goal/Title: {title}")

    return ChatMessage(
        role=ChatMessageRole.INFO,
        content="\n".join(lines),
        metadata={"count": len(flows)},
    )


def render_health(data: Any) -> ChatMessage:
    """Render a Gateway /health payload into an INFO ChatMessage."""
    if not isinstance(data, dict):
        return ChatMessage(role=ChatMessageRole.INFO, content="Gateway: нет данных о здоровье")
    status = str(data.get("status") or data.get("state") or "unknown")
    extras = ", ".join(
        f"{k}: {v}"
        for k, v in data.items()
        if k not in ("status", "state") and not isinstance(v, (dict, list))
    )
    text = f"Gateway: {status}"
    if extras:
        text += f" ({extras})"
    return ChatMessage(
        role=ChatMessageRole.INFO,
        content=text,
        metadata={"status": status},
    )


def render_commands_list(data: Any) -> ChatMessage:
    """Render a Gateway command catalog (list of command dicts) into INFO."""
    if isinstance(data, dict):
        for key in ("commands", "items", "entries"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
    if not isinstance(data, list) or not data:
        return ChatMessage(role=ChatMessageRole.INFO, content="Команды Gateway: пусто")
    lines = [f"Команды Gateway ({len(data)}):"]
    for cmd in data:
        if not isinstance(cmd, dict):
            continue
        name = str(cmd.get("name") or cmd.get("command") or "?")
        desc = str(cmd.get("description") or "")
        usage = str(cmd.get("usage") or "")
        line = f"  /{name}"
        if desc:
            line += f" — {desc}"
        if usage:
            line += f"   ({usage})"
        lines.append(line)
    return ChatMessage(
        role=ChatMessageRole.INFO,
        content="\n".join(lines),
        metadata={"count": len(data)},
    )


def render_memory_list(data: Any) -> ChatMessage:
    """Render the unified memory payload into an INFO ChatMessage."""
    entries: list[Any] = []
    if isinstance(data, dict):
        for key in ("entries", "items", "memories"):
            if isinstance(data.get(key), list):
                entries = data[key]
                break
        if not entries and "content" in data:
            entries = [data]
    elif isinstance(data, list):
        entries = data
    if not entries:
        return ChatMessage(role=ChatMessageRole.INFO, content="Память агента: пусто")
    lines = [f"Память агента ({len(entries)}):"]
    for entry in entries:
        if isinstance(entry, dict):
            content = str(entry.get("content") or entry.get("text") or "")
            kind = str(entry.get("kind") or "")
            lines.append(f"  • [{kind}] {content}" if kind else f"  • {content}")
        else:
            lines.append(f"  • {entry}")
    return ChatMessage(
        role=ChatMessageRole.INFO,
        content="\n".join(lines),
        metadata={"count": len(entries)},
    )


def render_session_info(data: Any) -> ChatMessage:
    """Render a session-info payload into an INFO ChatMessage."""
    if not isinstance(data, dict):
        return ChatMessage(role=ChatMessageRole.INFO, content="Сессия: нет данных")
    sid = str(data.get("session_id") or data.get("id") or "?")
    status = str(data.get("status") or data.get("state") or "unknown")
    lines = [f"Сессия [{sid}] status: {status}"]
    for key in ("model", "flow_ids", "created_at", "updated_at"):
        if key in data and not isinstance(data[key], (dict, list)):
            lines.append(f"  {key}: {data[key]}")
    return ChatMessage(
        role=ChatMessageRole.INFO,
        content="\n".join(lines),
        metadata={"session_id": sid},
    )


def render_session_history(data: Any) -> ChatMessage:
    """Render a session-history payload into an INFO ChatMessage."""
    messages: list[Any] = []
    if isinstance(data, dict):
        for key in ("messages", "history", "items"):
            if isinstance(data.get(key), list):
                messages = data[key]
                break
    elif isinstance(data, list):
        messages = data
    if not messages:
        return ChatMessage(role=ChatMessageRole.INFO, content="История сессии: пусто")
    lines = [f"История сессии ({len(messages)} сообщений):"]
    for msg in messages[:20]:
        if isinstance(msg, dict):
            role = str(msg.get("role") or "?")
            content = str(msg.get("content") or "")
            lines.append(f"  {role}: {content[:200]}")
        else:
            lines.append(f"  • {msg}")
    if len(messages) > 20:
        lines.append(f"  … и ещё {len(messages) - 20}")
    return ChatMessage(
        role=ChatMessageRole.INFO,
        content="\n".join(lines),
        metadata={"count": len(messages)},
    )


def render_outcome(outcome: TerminalOutcome) -> ChatMessage:
    """Render a typed TerminalOutcome into an assistant or error ChatMessage."""
    if outcome.is_success():
        res_str = str(outcome.result_data) if outcome.result_data is not None else "Completed successfully."
        return ChatMessage(
            role=ChatMessageRole.ASSISTANT,
            content=f"Result: {res_str}",
            metadata={"status": str(outcome.status)},
        )

    error_msg = outcome.error_message or f"Execution ended with status: {outcome.status}"
    role = ChatMessageRole.ERROR if outcome.status in (
        TerminalOutcomeStatus.FAILED,
        TerminalOutcomeStatus.ERROR,
        TerminalOutcomeStatus.MALFORMED,
    ) else ChatMessageRole.WARNING

    return ChatMessage(
        role=role,
        content=error_msg,
        metadata={"status": str(outcome.status)},
    )
