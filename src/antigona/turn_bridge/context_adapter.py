"""Context adapter — prepare task context for the TurnEngine.

Builds a system-level message that summarises the task's goal, acceptance
criteria, current plan, evidence, and facts so the LLM has full situational
awareness at every turn.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class TaskContext:
    """Rich task context for the TurnEngine.

    Attributes:
        goal: The high-level natural-language goal.
        acceptance_criteria: List of success criteria.
        current_plan: The current execution plan description.
        evidence: Dict of evidence accumulated so far (key → value).
        facts: List of established facts.
    """

    goal: str
    acceptance_criteria: list[str] = field(default_factory=list)
    current_plan: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    facts: list[str] = field(default_factory=list)


def prepare_task_context(
    goal: str,
    acceptance_criteria: list[str] | None = None,
    current_plan: str | None = None,
    evidence: dict[str, Any] | None = None,
    facts: list[str] | None = None,
) -> list[dict[str, str]]:
    """Build the initial messages from a task context.

    Produces a system message with the goal, criteria, plan, and evidence,
    followed by a user message with any established facts.

    Args:
        goal: The task goal.
        acceptance_criteria: Optional list of success criteria.
        current_plan: Optional description of the current plan.
        evidence: Optional dict of accumulated evidence.
        facts: Optional list of established facts.

    Returns:
        A list of message dicts (``system``, then ``user``) ready to
        prepend to the conversation.
    """
    system_parts = [
        f"# Goal\n\n{goal}",
    ]
    if acceptance_criteria:
        criteria_lines = "\n".join(f"- {c}" for c in acceptance_criteria)
        system_parts.append(f"## Acceptance Criteria\n\n{criteria_lines}")
    if current_plan:
        system_parts.append(f"## Current Plan\n\n{current_plan}")
    if evidence:
        evidence_lines = "\n".join(f"- {k}: {v}" for k, v in evidence.items())
        system_parts.append(f"## Evidence\n\n{evidence_lines}")

    system_content = "\n\n".join(system_parts)
    messages: list[dict[str, str]] = [
        {"role": "system", "content": system_content},
    ]

    if facts:
        facts_content = "\n".join(f"- {f}" for f in facts)
        messages.append({"role": "user", "content": f"## Established Facts\n\n{facts_content}"})

    return messages


def build_system_prompt(task_context: TaskContext) -> str:
    """Build a single system prompt string from a :class:`TaskContext`.

    Args:
        task_context: The rich task context.

    Returns:
        A complete system prompt string.
    """
    parts = [
        f"# Goal\n\n{task_context.goal}",
    ]
    if task_context.acceptance_criteria:
        criteria_lines = "\n".join(f"- {c}" for c in task_context.acceptance_criteria)
        parts.append(f"## Acceptance Criteria\n\n{criteria_lines}")
    if task_context.current_plan:
        parts.append(f"## Current Plan\n\n{task_context.current_plan}")
    if task_context.evidence:
        evidence_lines = "\n".join(f"- {k}: {v}" for k, v in task_context.evidence.items())
        parts.append(f"## Evidence\n\n{evidence_lines}")
    if task_context.facts:
        facts_lines = "\n".join(f"- {f}" for f in task_context.facts)
        parts.append(f"## Established Facts\n\n{facts_lines}")
    return "\n\n".join(parts)
