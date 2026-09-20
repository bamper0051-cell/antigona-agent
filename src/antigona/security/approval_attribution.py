"""FP-L09 — every APPROVED approval names its deciding subject.

Live defect (canon ``1ea7727``): ``TaskRepository.request_approval`` wrote
``decision='APPROVED'`` for a policy auto-approval while leaving
``decided_by``/``decided_at`` NULL. The manual branch always recorded them, so
the audit trail contained two visually identical kinds of "APPROVED" rows:
the ones the owner decided and the ones nobody decided. An approval with no subject
is not an audit record — it cannot be told apart from a forged one. Live count:
38 such rows on the first read and 52 by the time of the fix (2026-09-18) —
the table grows by one anonymous row per LOW auto-approval.

Design (chosen over a new ``approvals`` column, and justified):

* the subject is an explicit, machine-checkable marker —
  ``auto:<policy>:<RISK>`` — so it is verifiable with plain SQL
  (``decided_by LIKE 'auto:%'``) on every already-provisioned database,
  including the live one, without a migration, without an Alembic head bump and
  without touching the frozen flat-migration chain;
* an owner decision keeps its existing form: ``decided_by=<owner id>``. The two
  are therefore trivially separable, and the auto marker can never collide with
  an owner id (Telegram owner ids are numeric);
* ``decided_at`` is always set for both, so the row records WHEN as well;
* the invariant "no ``APPROVED`` without a non-empty ``decided_by``" is enforced
  fail-closed at the write path (:func:`assert_attributed`), not merely asserted
  in tests.

Execution contract is deliberately untouched: an auto approval still carries no
``grant_token``, and ``grant_token``/gate checks are unchanged — the orchestrator
and the worker keep treating a grant-less APPROVED row as policy auto-approval.
"""

from __future__ import annotations

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

#: Marks a decision taken by the confirmation policy instead of the owner.
AUTO_SUBJECT_PREFIX = "auto:"
#: Names the deciding component so the marker stays readable if more policies
#: ever gain auto-approval authority.
AUTO_POLICY_NAME = "confirmation_policy"
#: Decisions that MUST name a subject.
ATTRIBUTED_DECISIONS = frozenset({"APPROVED"})


class UnattributedApprovalError(RuntimeError):
    """An APPROVED approval was about to be persisted without a subject."""


def auto_approval_subject(risk_level: object) -> str:
    """Return the canonical subject of a policy auto-approval.

    ``auto:confirmation_policy:<RISK>`` — e.g. ``auto:confirmation_policy:LOW``.
    """
    risk = str(getattr(risk_level, "value", risk_level) or "").strip().upper() or "UNKNOWN"
    return f"{AUTO_SUBJECT_PREFIX}{AUTO_POLICY_NAME}:{risk}"


def is_auto_subject(subject: object | None) -> bool:
    """True when *subject* marks a policy auto-approval (never an owner id)."""
    return str(subject or "").strip().startswith(AUTO_SUBJECT_PREFIX)


def auto_approval_authority(policy: object) -> str:
    """Readable authority of an auto-approval, for the approval ``reason`` line.

    ``confirmation_policy mode=<MODE>`` — the mode is what actually decided, so
    an audit can tell an expected ``ALWAYS`` auto-approval from an unexpected
    one under ``NEVER``/``HIGH_ONLY``.
    """
    mode = getattr(getattr(policy, "mode", None), "value", None)
    if mode is None:
        mode = getattr(policy, "mode", None)
    return f"{AUTO_POLICY_NAME} mode={str(mode or 'unknown')}"


def subject_is_attributed(decision: object | None, decided_by: object | None) -> bool:
    """True when the (decision, subject) pair is auditable.

    A row that is not in :data:`ATTRIBUTED_DECISIONS` (PENDING/DENIED) carries no
    claim of authority, so it needs no subject.
    """
    if str(decision or "").strip().upper() not in ATTRIBUTED_DECISIONS:
        return True
    return bool(str(decided_by or "").strip())


def assert_attributed(
    decision: object | None, decided_by: object | None, *, approval_id: str = ""
) -> None:
    """Fail closed on an anonymous APPROVED row (the FP-L09 invariant)."""
    if subject_is_attributed(decision, decided_by):
        return
    where = f" (approval {approval_id})" if approval_id else ""
    raise UnattributedApprovalError(
        f"approval{where} would be persisted as APPROVED without decided_by: "
        "an approval with no subject is not an audit record"
    )


def unattributed_approved_ids(session: Session) -> list[str]:
    """Audit query: ids of APPROVED approvals that name no subject.

    Empty on a clean database. Exists so the invariant is verifiable by owner
    audit (and by tests) against a real table instead of only in memory.
    """
    from antigona.models import Approval

    rows = session.execute(
        select(Approval.id).where(
            Approval.decision == "APPROVED",
            or_(Approval.decided_by.is_(None), func.trim(Approval.decided_by) == ""),
        )
    ).scalars()
    return [str(row) for row in rows]
