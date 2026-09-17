"""Hermes RCA — CLI diagnostic overlay + /hermes commands (spec sec 10-12, 25).

The overlay is a thin diagnostic panel drawn on top of the existing CLI; it
never clears user input, never re-renders the full terminal, and never blocks
the main flow. Commands are wired through the existing command architecture
(no separate router).
"""

from __future__ import annotations

from typing import Any

from antigona.rca.result import RCAStatus


def render_overlay(
    *,
    error: str = "",
    component: str = "",
    task_id: str = "",
    status: str = RCAStatus.DIAGNOSING.value,
    root_cause: str = "",
    confidence: str = "",
    impact: str = "",
    action: str = "",
) -> str:
    """Build a compact, non-intrusive RCA panel (spec sections 10, 25).

    Returns a plain-text panel that the CLI can splice into its output without
    disrupting the current line/cursor. Rendering is pure text (no full-screen
    redraw), so it satisfies the CLI UX constraints in section 11.
    """
    width = max(len("HERMES RCA"), len(f"ERROR        {error}"),
                len(f"ROOT CAUSE   {root_cause}"), len(f"CONFIDENCE   {confidence}"),
                len(f"IMPACT       {impact}"), len(f"ACTION       {action}"), 28) + 4
    line = "=" * width
    box = [
        line,
        "HERMES RCA".ljust(width - 1) + " ",
        line,
    ]
    if error:
        box.append(f"ERROR        {error}")
    if component:
        box.append(f"COMPONENT    {component}")
    if task_id:
        box.append(f"TASK         {task_id}")
    box.append(f"STATUS       {status}")
    if root_cause:
        box.append(f"ROOT CAUSE   {root_cause}")
    if confidence:
        box.append(f"CONFIDENCE   {confidence}")
    if impact:
        box.append(f"IMPACT       {impact}")
    if action:
        box.append(f"ACTION       {action}")
    box.append(line)
    return "\n".join(box)


def format_result_text(data: dict[str, Any]) -> str:
    """Render a persisted RCAResult as readable text for /hermes last/trace."""
    lines = [
        f"rca_id       {data.get('rca_id', '')}",
        f"error_id     {data.get('error_id', '')}",
        f"category     {data.get('category', 'UNKNOWN')}",
        f"confidence   {data.get('confidence', 'LOW')}",
        f"status       {data.get('status', '')}",
        f"component    {data.get('source_component', '')}",
    ]
    if data.get("root_cause"):
        lines.append(f"root cause   {data['root_cause']}")
    if data.get("user_impact"):
        lines.append(f"impact       {data['user_impact']}")
    if data.get("exception_type"):
        lines.append(f"exception    {data['exception_type']}: {data.get('error_message','')}")
    if data.get("duplicate_count", 1) > 1:
        lines.append(f"dedup        same failure x{data['duplicate_count']}")
    if data.get("recommended_actions"):
        lines.append("actions:")
        for a in data["recommended_actions"]:
            lines.append(f"  - {a}")
    return "\n".join(lines)


def format_explain(data: dict[str, Any]) -> str:
    """Render a full RCA record as an explain panel (evidence + full context).

    Read-only: expands every diagnostic field stored for the error_id so the
    owner can review the evidence chain before deciding on remediation.
    """
    lines = ["🤖 Hermes RCA — детальный разбор ошибки", "=" * 52]
    lines.append(f"rca_id         {data.get('rca_id', '')}")
    lines.append(f"error_id       {data.get('error_id', '')}")
    lines.append(f"correlation_id {data.get('correlation_id', '')}")
    lines.append(f"category       {data.get('category', 'UNKNOWN')}")
    lines.append(f"confidence     {data.get('confidence', 'LOW')}")
    lines.append(f"status         {data.get('status', '')}")
    lines.append(f"severity       {data.get('severity', '')}")
    lines.append(f"component      {data.get('source_component', '')}")
    if data.get("tool_name"):
        lines.append(f"tool           {data['tool_name']}")
    if data.get("provider"):
        lines.append(f"provider       {data['provider']}")
    if data.get("model"):
        lines.append(f"model          {data['model']}")
    if data.get("task_id"):
        lines.append(f"task/flow/step {data.get('task_id')}/{data.get('flow_id') or '-'}/{data.get('step_id') or '-'}")
    if data.get("exception_type"):
        lines.append(f"exception      {data['exception_type']}: {data.get('error_message', '')}")
    if data.get("root_cause"):
        lines.append(f"root cause     {data['root_cause']}")
    if data.get("user_impact"):
        lines.append(f"impact         {data['user_impact']}")
    if data.get("summary"):
        lines.append(f"summary        {data['summary']}")
    if data.get("git_revision"):
        lines.append(f"git revision   {data['git_revision']}")
    if data.get("duplicate_count", 1) > 1:
        lines.append(f"dedup          same failure x{data['duplicate_count']}")
    ev = data.get("evidence") or []
    if ev:
        lines.append("-" * 52)
        lines.append("evidence:")
        for e in ev:
            if isinstance(e, dict):
                lines.append(f"  - {e.get('kind', '?')}: {e.get('value', '')}")
            else:
                lines.append(f"  - {e}")
    acts = data.get("recommended_actions") or []
    if acts:
        lines.append("-" * 52)
        lines.append("remediation (data only, requires owner approval):")
        for a in acts:
            lines.append(f"  - {a}")
    lines.append("=" * 52)
    return "\n".join(lines)


def format_evidence(data: dict[str, Any]) -> str:
    """Render the evidence chain for an RCA verdict (read-only).

    Shows the correlated evidence collected during diagnosis: exception type,
    message, source component, tool, provider, policy decision, and the sanitized
    stack head. Never includes raw credentials (evidence is redacted at capture).
    """
    lines = ["🔎 Hermes RCA — evidence", "=" * 52]
    lines.append(f"rca_id         {data.get('rca_id', '')}")
    lines.append(f"error_id       {data.get('error_id', '')}")
    lines.append(f"correlation_id {data.get('correlation_id', '')}")
    lines.append(f"category       {data.get('category', 'UNKNOWN')}")
    lines.append(f"confidence     {data.get('confidence', 'LOW')}")
    ev = data.get("evidence") or []
    if not ev:
        lines.append("evidence: (none collected)")
    else:
        lines.append("-" * 52)
        lines.append(f"evidence ({len(ev)} items):")
        for e in ev:
            if isinstance(e, dict):
                lines.append(f"  · {e.get('kind', '?')}: {e.get('value', '')}")
            else:
                lines.append(f"  · {e}")
    lines.append("=" * 52)
    return "\n".join(lines)


def format_suggest_fix(data: dict[str, Any]) -> str:
    """Render the remediation suggestions for an RCA verdict (data only).

    Read-only: shows recommended actions plus the safety gate (safe_to_auto_fix
    is always False; owner approval is always required). Hermes never applies
    these changes itself.
    """
    lines = ["🛠 Hermes RCA — suggested fix", "=" * 52]
    lines.append(f"rca_id         {data.get('rca_id', '')}")
    lines.append(f"error_id       {data.get('error_id', '')}")
    lines.append(f"correlation_id {data.get('correlation_id', '')}")
    lines.append(f"category       {data.get('category', 'UNKNOWN')}")
    lines.append(f"confidence     {data.get('confidence', 'LOW')}")
    if data.get("root_cause"):
        lines.append(f"root cause     {data['root_cause']}")
    if data.get("user_impact"):
        lines.append(f"impact         {data['user_impact']}")
    lines.append("-" * 52)
    acts = data.get("recommended_actions") or []
    if not acts:
        lines.append("suggested actions: (none)")
    else:
        lines.append("suggested actions (data only — Hermes does not apply changes):")
        for a in acts:
            lines.append(f"  • {a}")
    safe = data.get("safe_to_auto_fix", False)
    lines.append("-" * 52)
    lines.append(f"safe_to_auto_fix        {safe}")
    lines.append(f"requires_owner_approval {data.get('requires_owner_approval', True)}")
    lines.append("=" * 52)
    return "\n".join(lines)

