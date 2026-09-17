"""Unit tests: AgentCore._has_approved_shell_approval.

The decision to route a high-risk command to the Docker sandbox depends on an
APPROVED approval on the current task. PENDING or no approval => not approved =>
high-risk command stays refused.
"""

from __future__ import annotations

from types import SimpleNamespace

from antigona.worker.agent_core import WorkerAgentCore


def _task_with_approvals(*approvals) -> SimpleNamespace:
    return SimpleNamespace(approvals=list(approvals), tool_name="sandbox.shell")


def _approval(decision: str, tool_name: str = "sandbox.shell") -> SimpleNamespace:
    return SimpleNamespace(tool_name=tool_name, decision=decision)


def test_no_task_is_not_approved() -> None:
    core = object.__new__(WorkerAgentCore)
    core._current_task = None
    assert core._has_approved_shell_approval() is False


def test_approved_approval_grants_sandbox() -> None:
    core = object.__new__(WorkerAgentCore)
    core._current_task = _task_with_approvals(_approval("APPROVED"))
    assert core._has_approved_shell_approval() is True


def test_pending_only_is_not_approved() -> None:
    core = object.__new__(WorkerAgentCore)
    core._current_task = _task_with_approvals(_approval("PENDING"))
    assert core._has_approved_shell_approval() is False


def test_approved_for_different_tool_is_not_shell_approved() -> None:
    core = object.__new__(WorkerAgentCore)
    core._current_task = _task_with_approvals(_approval("APPROVED", tool_name="send_file"))
    assert core._has_approved_shell_approval() is False
