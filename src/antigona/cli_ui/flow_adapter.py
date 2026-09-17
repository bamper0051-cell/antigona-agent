"""Validated boundary adapter: authoritative ``FlowStatus`` → presentation outcome.

This module is the *only* place in the CLI presentation stack that is allowed to
read the authoritative Core/Gateway ``FlowStatus``.  It converts a validated flow
status into a typed :class:`~antigona.cli_ui.models.TerminalOutcome`.

Contract direction::

    FlowStatus.DONE -> adapt_flow_status() -> TerminalOutcomeStatus.SUCCESS
                    -> TerminalOutcome.is_success() -> CliRenderer success panel

``FlowStatus.DONE`` is the single authoritative source of success.  Everything
else fails closed: the other nineteen authoritative statuses map to explicit
non-success outcomes, and raw API strings (including the literal ``"DONE"``),
``None`` and malformed values become ``TerminalOutcomeStatus.MALFORMED``.

``antigona.cli_ui.models`` and ``antigona.cli_ui.renderer`` deliberately know
nothing about ``FlowStatus``, ``DONE`` or any wire vocabulary — they only ever see
the typed presentation enum.  That asymmetry is what makes the success gate
un-spoofable from the wire, and it is enforced by
``tests/unit/test_cli_ui_flow_contract.py``.

Core status definitions are consumed read-only; this module never redefines or
extends them.
"""

from __future__ import annotations

from typing import Any, Final

from antigona.cli_ui.models import TerminalOutcome, TerminalOutcomeStatus
from antigona.core.control_plane import FlowStatus

#: The one authoritative status that may open the presentation success gate.
AUTHORITATIVE_SUCCESS_STATUS: Final[FlowStatus] = FlowStatus.DONE

#: Total, explicit classification of every non-success authoritative status.
#: Kept exhaustive on purpose: a status added to Core lands in neither this map
#: nor the success slot, so the contract test fails loudly instead of a silent
#: default quietly classifying an unknown lifecycle state.
NON_SUCCESS_STATUS_MAP: Final[dict[FlowStatus, TerminalOutcomeStatus]] = {
    # In-flight lifecycle: accepted by the Gateway, not terminal, not success.
    FlowStatus.RECEIVED: TerminalOutcomeStatus.ACCEPTED,
    FlowStatus.CREATED: TerminalOutcomeStatus.ACCEPTED,
    FlowStatus.PLANNING: TerminalOutcomeStatus.ACCEPTED,
    FlowStatus.READY: TerminalOutcomeStatus.ACCEPTED,
    FlowStatus.QUEUED: TerminalOutcomeStatus.QUEUED,
    FlowStatus.RUNNING: TerminalOutcomeStatus.ACCEPTED,
    FlowStatus.TOOL_EXECUTING: TerminalOutcomeStatus.ACCEPTED,
    FlowStatus.OBSERVING: TerminalOutcomeStatus.ACCEPTED,
    FlowStatus.PAUSED: TerminalOutcomeStatus.ACCEPTED,
    FlowStatus.RETRY_SCHEDULED: TerminalOutcomeStatus.ACCEPTED,
    FlowStatus.REPLAN_REQUESTED: TerminalOutcomeStatus.ACCEPTED,
    # VERIFYING is the state DONE is reachable from — it is not itself success.
    FlowStatus.VERIFYING: TerminalOutcomeStatus.ACCEPTED,
    # Blocked on a human decision.
    FlowStatus.WAITING_USER: TerminalOutcomeStatus.WAITING_APPROVAL,
    FlowStatus.WAITING_APPROVAL: TerminalOutcomeStatus.WAITING_APPROVAL,
    # Terminal, non-success.
    FlowStatus.FAILED: TerminalOutcomeStatus.FAILED,
    FlowStatus.TIMEOUT: TerminalOutcomeStatus.TIMEOUT,
    FlowStatus.POLICY_DENIED: TerminalOutcomeStatus.REJECTED,
    FlowStatus.BLOCKED: TerminalOutcomeStatus.REJECTED,
    FlowStatus.CANCELLED: TerminalOutcomeStatus.CANCELLED,
}


def adapt_flow_status(
    status: Any,
    *,
    result_data: Any = None,
    error_message: str | None = None,
) -> TerminalOutcome:
    """Translate an authoritative flow status into a typed terminal outcome.

    Only ``FlowStatus.DONE`` yields ``TerminalOutcomeStatus.SUCCESS`` and carries
    ``result_data`` forward.  A non-``FlowStatus`` value — a raw API string, ``None``,
    or anything else — is rejected as ``MALFORMED`` without inspecting its text, so
    a wire payload spelling ``"DONE"`` or ``"SUCCESS"`` can never become success.
    """
    if not isinstance(status, FlowStatus):
        return TerminalOutcome(
            status=TerminalOutcomeStatus.MALFORMED,
            error_message=error_message
            or (
                "Flow status was not a validated FlowStatus member "
                f"(received {type(status).__name__}); refusing to interpret it."
            ),
        )

    if status is AUTHORITATIVE_SUCCESS_STATUS:
        return TerminalOutcome(
            status=TerminalOutcomeStatus.SUCCESS,
            result_data=result_data,
            error_message=None,
        )

    mapped = NON_SUCCESS_STATUS_MAP.get(status)
    if mapped is None:
        return TerminalOutcome(
            status=TerminalOutcomeStatus.MALFORMED,
            error_message=error_message
            or (
                f"Flow status {status.value} is not classified by the presentation "
                "adapter; refusing to guess a terminal outcome."
            ),
        )

    return TerminalOutcome(
        status=mapped,
        result_data=None,
        error_message=error_message or f"Flow status {status.value} is not a terminal success.",
    )


__all__ = [
    "AUTHORITATIVE_SUCCESS_STATUS",
    "NON_SUCCESS_STATUS_MAP",
    "adapt_flow_status",
]
