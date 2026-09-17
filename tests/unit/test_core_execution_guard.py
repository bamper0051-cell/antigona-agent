"""P2 REWORK: central execution-claim evidence guard — negative matrix.

Every execution-claiming transition (TOOL_EXECUTING, OBSERVING, VERIFYING,
DONE) must be REFUSED before CAS/append when evidence is missing, unknown,
foreign, non-VERIFIED (PROPOSED/OBSERVED/REJECTED), MODEL_OUTPUT, duplicated,
or backed by an approval claim from another task.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from antigona.core.evidence_registry import (
    EvidenceKind,
    EvidenceRegistry,
    EvidenceSource,
    ExecutionOutcome,
    InvalidEvidenceTransition,
)
from antigona.core.task_registry import TaskRegistry, TaskRegistryError
from antigona.database import Database
from antigona.durable.state_machine import InvalidTransition
from antigona.models import TaskState

EXECUTION_TARGETS = (
    TaskState.TOOL_EXECUTING,
    TaskState.OBSERVING,
    TaskState.VERIFYING,
)


def _make_db(tmp_path: Path, name: str) -> tuple[Database, str]:
    db = Database(f"sqlite:///{tmp_path / name}")
    db.create_all()
    with db.session_factory() as session:
        registry = TaskRegistry(session)
        created = registry.create(
            owner_id="owner-1",
            goal="guard",
            idempotency_key=f"idem-{name}",
            correlation_id="corr-1",
        )
        session.commit()
        return db, created.task_id


def _verified(
    session: object, evidence_id: str, task_id: str, *,
    source: EvidenceSource = EvidenceSource.WORKER, claim: str = "exec:run",
    kind: EvidenceKind = EvidenceKind.READINESS,
    outcome: ExecutionOutcome | None = None,
    correlation_id: str = "",
) -> None:
    er = EvidenceRegistry(session)  # type: ignore[arg-type]
    er.register(
        evidence_id=evidence_id, task_id=task_id, attempt_id="a1",
        type="worker_exec", source=source, supports_claims=(claim,),
        kind=kind, outcome=outcome, correlation_id=correlation_id,
    )
    er.observe(evidence_id, artifact_reference="logs/1", sha256="0" * 64)
    er.verify(evidence_id, verified_by="verifier")


@pytest.mark.parametrize("target", EXECUTION_TARGETS)
def test_transition_into_execution_state_without_evidence_refused(
    tmp_path: Path, target: TaskState
) -> None:
    db, task_id = _make_db(tmp_path, f"no_evidence_{target.value}")
    with db.session_factory() as session:
        r = TaskRegistry(session)
        with pytest.raises(TaskRegistryError, match="requires verified evidence_refs"):
            r.update_status(task_id, target, actor="worker", correlation_id="corr-x")


@pytest.mark.parametrize("target", EXECUTION_TARGETS)
def test_unknown_evidence_refused(tmp_path: Path, target: TaskState) -> None:
    db, task_id = _make_db(tmp_path, f"unknown_{target.value}")
    with db.session_factory() as session:
        r = TaskRegistry(session)
        with pytest.raises(TaskRegistryError, match="unknown or belong to another task"):
            r.update_status(
                task_id, target, actor="worker", correlation_id="corr-x",
                evidence_refs=("no-such-evidence",),
            )


@pytest.mark.parametrize("target", EXECUTION_TARGETS)
def test_foreign_task_evidence_refused(tmp_path: Path, target: TaskState) -> None:
    db, task_id = _make_db(tmp_path, f"foreign_{target.value}")
    with db.session_factory() as session:
        _verified(session, "ev-foreign", "other-task-id")
        r = TaskRegistry(session)
        with pytest.raises(TaskRegistryError, match="unknown or belong to another task"):
            r.update_status(
                task_id, target, actor="worker", correlation_id="corr-x",
                evidence_refs=("ev-foreign",),
            )


@pytest.mark.parametrize(
    "status_name, stage",
    [("proposed", "none"), ("observed", "observe"), ("rejected", "reject")],
)
def test_non_verified_evidence_refused(
    tmp_path: Path, status_name: str, stage: str
) -> None:
    db, task_id = _make_db(tmp_path, f"non_verified_{status_name}")
    with db.session_factory() as session:
        er = EvidenceRegistry(session)
        er.register(
            evidence_id="ev-nv", task_id=task_id, attempt_id="a1",
            type="worker_exec", source=EvidenceSource.WORKER,
            supports_claims=("exec:run",),
        )
        if stage == "observe":
            er.observe("ev-nv", artifact_reference="logs/1", sha256="0" * 64)
        if stage == "reject":
            er.reject("ev-nv", reason="bad")
        r = TaskRegistry(session)
        with pytest.raises(TaskRegistryError, match="VERIFIED required"):
            r.update_status(
                task_id, TaskState.TOOL_EXECUTING, actor="worker",
                correlation_id="corr-x", evidence_refs=("ev-nv",),
            )


def test_model_output_evidence_can_never_back_execution_claim(tmp_path: Path) -> None:
    db, task_id = _make_db(tmp_path, "model_output")
    with db.session_factory() as session:
        er = EvidenceRegistry(session)
        er.register(
            evidence_id="ev-model", task_id=task_id, attempt_id="a1",
            type="llm_output", source=EvidenceSource.MODEL_OUTPUT,
            supports_claims=("summary",),
        )
        # hard rule: MODEL_OUTPUT cannot reach VERIFIED at all
        with pytest.raises(InvalidEvidenceTransition):
            er.observe("ev-model", artifact_reference="x", sha256="0" * 64)
            er.verify("ev-model", verified_by="verifier")
        r = TaskRegistry(session)
        with pytest.raises(TaskRegistryError):
            r.update_status(
                task_id, TaskState.TOOL_EXECUTING, actor="worker",
                correlation_id="corr-x", evidence_refs=("ev-model",),
            )


def test_duplicated_evidence_refs_refused(tmp_path: Path) -> None:
    db, task_id = _make_db(tmp_path, "duplicated")
    with db.session_factory() as session:
        _verified(session, "ev-dup", task_id)
        r = TaskRegistry(session)
        with pytest.raises(TaskRegistryError, match="duplicated evidence_refs"):
            r.update_status(
                task_id, TaskState.TOOL_EXECUTING, actor="worker",
                correlation_id="corr-x", evidence_refs=("ev-dup", "ev-dup"),
            )


def test_approval_evidence_from_other_task_refused_for_approval_gate(
    tmp_path: Path,
) -> None:
    """WAITING_APPROVAL -> TOOL_EXECUTING requires an OWNER_INPUT approval
    claim belonging to THIS task; a foreign approval must fail."""
    db, task_id = _make_db(tmp_path, "foreign_approval")
    with db.session_factory() as session:
        _verified(session, "ev-foreign-approval", "other-task",
                  source=EvidenceSource.OWNER_INPUT, claim="approval:other-approval-id",
                  kind=EvidenceKind.APPROVAL_DECISION)
        _verified(session, "ev-mine", task_id)
        r = TaskRegistry(session)
        r.update_status(task_id, TaskState.QUEUED, actor="gateway", correlation_id="corr-q")
        r.update_status(task_id, TaskState.PLANNING, actor="worker", correlation_id="corr-p")
        r.update_status(task_id, TaskState.WAITING_APPROVAL, actor="worker",
                        correlation_id="corr-wa")
        with pytest.raises(TaskRegistryError, match="unknown or belong to another task"):
            r.update_status(
                task_id, TaskState.TOOL_EXECUTING, actor="worker",
                correlation_id="corr-x",
                evidence_refs=("ev-mine", "ev-foreign-approval"),
            )


def test_done_requires_verified_evidence_and_verifier_capability(tmp_path: Path) -> None:
    db, task_id = _make_db(tmp_path, "done_guard")
    with db.session_factory() as session:
        _verified(session, "ev-done", task_id,
                  kind=EvidenceKind.EXECUTION_RESULT,
                  outcome=ExecutionOutcome.SUCCESS,
                  correlation_id="corr-done-op")
        r = TaskRegistry(session)
        r.update_status(task_id, TaskState.QUEUED, actor="gateway", correlation_id="corr-q")
        r.update_status(task_id, TaskState.PLANNING, actor="worker", correlation_id="corr-p")
        r.update_status(task_id, TaskState.READY, actor="worker", correlation_id="corr-r")
        r.update_status(task_id, TaskState.TOOL_EXECUTING, actor="worker",
                        correlation_id="corr-e1", evidence_refs=("ev-done",))
        r.update_status(task_id, TaskState.OBSERVING, actor="worker",
                        correlation_id="corr-o1", evidence_refs=("ev-done",))
        r.update_status(task_id, TaskState.VERIFYING, actor="worker",
                        correlation_id="corr-v1", evidence_refs=("ev-done",))
        # DONE with unknown evidence -> refused (unknown evidence category)
        with pytest.raises(TaskRegistryError, match="unknown or belong to another task"):
            r.update_status(
                task_id, TaskState.DONE, actor="verifier",
                correlation_id="corr-d1", evidence_refs=("missing-ev",),
                verifier_capability=True,
            )
        # DONE with non-VERIFIED evidence -> refused (VERIFIED required)
        er2 = EvidenceRegistry(session)
        er2.register(
            evidence_id="ev-proposed", task_id=task_id, attempt_id="a2",
            type="worker_exec", source=EvidenceSource.WORKER,
            supports_claims=("exec:run",),
        )
        with pytest.raises(TaskRegistryError, match="VERIFIED required"):
            r.update_status(
                task_id, TaskState.DONE, actor="verifier",
                correlation_id="corr-d3", evidence_refs=("ev-proposed",),
                verifier_capability=True,
            )
        # DONE without verifier capability -> refused by state machine
        # (evidence passes the guard: correlation matches; capability fails)
        with pytest.raises(InvalidTransition):
            r.update_status(
                task_id, TaskState.DONE, actor="worker",
                correlation_id="corr-done-op", evidence_refs=("ev-done",),
            )


def test_fabricated_approval_claim_without_approval_row_refused(
    tmp_path: Path,
) -> None:
    """Adversarial F2: OWNER_INPUT evidence with a fake claim 'approval:fake'
    (no Approval row exists) must NOT unlock WAITING_APPROVAL->TOOL_EXECUTING."""

    db, task_id = _make_db(tmp_path, "fake_claim")
    with db.session_factory() as session:
        _verified(session, "ev-fake-approval", task_id,
                  source=EvidenceSource.OWNER_INPUT, claim="approval:fake-approval-id",
                  kind=EvidenceKind.APPROVAL_DECISION)
        r = TaskRegistry(session)
        r.update_status(task_id, TaskState.QUEUED, actor="gateway", correlation_id="corr-q")
        r.update_status(task_id, TaskState.PLANNING, actor="worker", correlation_id="corr-p")
        r.update_status(task_id, TaskState.WAITING_APPROVAL, actor="worker",
                        correlation_id="corr-wa")
        with pytest.raises(TaskRegistryError, match="trusted OWNER_INPUT approval claim"):
            r.update_status(
                task_id, TaskState.TOOL_EXECUTING, actor="worker",
                correlation_id="corr-x", evidence_refs=("ev-fake-approval",),
            )


def test_approval_claim_for_other_task_refused(tmp_path: Path) -> None:
    """Adversarial F1: an APPROVED approval of ANOTHER task must not satisfy
    this task's gate even when the evidence row belongs to this task."""
    from antigona.models import Approval

    db, task_id = _make_db(tmp_path, "other_task_approval")
    with db.session_factory() as session:
        other = TaskRegistry(session)
        other_task = other.create(
            owner_id="owner-2", goal="other",
            idempotency_key="idem-other", correlation_id="corr-other",
        )
        other_approval = Approval(
            id="other-approval-1", task_id=other_task.task_id,
            tool_name="x", arguments={}, risk_level="SAFE",
            reason="other", decision="APPROVED",
        )
        session.add(other_approval)
        session.flush()
        _verified(session, "ev-other-approval", task_id,
                  source=EvidenceSource.OWNER_INPUT,
                  claim="approval:other-approval-1",
                  kind=EvidenceKind.APPROVAL_DECISION)
        r = TaskRegistry(session)
        r.update_status(task_id, TaskState.QUEUED, actor="gateway", correlation_id="corr-q")
        r.update_status(task_id, TaskState.PLANNING, actor="worker", correlation_id="corr-p")
        r.update_status(task_id, TaskState.WAITING_APPROVAL, actor="worker",
                        correlation_id="corr-wa")
        with pytest.raises(TaskRegistryError, match="trusted OWNER_INPUT approval claim"):
            r.update_status(
                task_id, TaskState.TOOL_EXECUTING, actor="worker",
                correlation_id="corr-x", evidence_refs=("ev-other-approval",),
            )


def test_real_approved_approval_satisfies_gate(tmp_path: Path) -> None:
    """Positive: a REAL APPROVED Approval row for THIS task + VERIFIED
    OWNER_INPUT evidence with its claim unlocks the gate."""
    from antigona.models import Approval

    db, task_id = _make_db(tmp_path, "real_approval")
    with db.session_factory() as session:
        approval = Approval(
            id="real-approval-1", task_id=task_id,
            tool_name="workspace.write_text", arguments={}, risk_level="SAFE",
            reason="ok", decision="APPROVED",
        )
        session.add(approval)
        session.flush()
        _verified(session, "ev-real-approval", task_id,
                  source=EvidenceSource.OWNER_INPUT,
                  claim="approval:real-approval-1",
                  kind=EvidenceKind.APPROVAL_DECISION)
        r = TaskRegistry(session)
        r.update_status(task_id, TaskState.QUEUED, actor="gateway", correlation_id="corr-q")
        r.update_status(task_id, TaskState.PLANNING, actor="worker", correlation_id="corr-p")
        r.update_status(task_id, TaskState.WAITING_APPROVAL, actor="worker",
                        correlation_id="corr-wa")
        rec = r.update_status(
            task_id, TaskState.TOOL_EXECUTING, actor="worker",
            correlation_id="corr-x", evidence_refs=("ev-real-approval",),
        )
        assert rec.status is TaskState.TOOL_EXECUTING


# ── P2 round 3: DONE transition-specific evidence policy ────────────────────

def _walk_to_verifying(session: object, task_id: str) -> None:  # type: ignore[no-untyped-def]
    """Walk an existing task to VERIFYING with a valid EXECUTION_RESULT
    evidence bound to operation correlation corr-done-op."""
    r = TaskRegistry(session)  # type: ignore[arg-type]
    r.update_status(task_id, TaskState.QUEUED, actor="gateway", correlation_id="corr-q")
    r.update_status(task_id, TaskState.PLANNING, actor="worker", correlation_id="corr-p")
    r.update_status(task_id, TaskState.READY, actor="worker", correlation_id="corr-r")
    _verified(session, "ev-exec", task_id,
              kind=EvidenceKind.EXECUTION_RESULT,
              outcome=ExecutionOutcome.SUCCESS, correlation_id="corr-done-op")
    r.update_status(task_id, TaskState.TOOL_EXECUTING, actor="worker",
                    correlation_id="corr-e", evidence_refs=("ev-exec",))
    r.update_status(task_id, TaskState.OBSERVING, actor="worker",
                    correlation_id="corr-o", evidence_refs=("ev-exec",))
    r.update_status(task_id, TaskState.VERIFYING, actor="worker",
                    correlation_id="corr-v", evidence_refs=("ev-exec",))


DONE_DISALLOWED_SOURCES = [
    ("OWNER_INPUT", EvidenceSource.OWNER_INPUT),
    ("TELEGRAM_UPDATE", EvidenceSource.TELEGRAM_UPDATE),
    ("SOURCE_CODE", EvidenceSource.SOURCE_CODE),
    ("GIT", EvidenceSource.GIT),
    ("TEST_RUNNER", EvidenceSource.TEST_RUNNER),
    ("STATIC_ANALYZER", EvidenceSource.STATIC_ANALYZER),
]


@pytest.mark.parametrize("label,source", DONE_DISALLOWED_SOURCES)
def test_done_refused_with_non_execution_authoritative_source(
    tmp_path: Path, label: str, source: EvidenceSource
) -> None:
    """Owner round 3: DONE only with VERIFIED <non-authoritative> must fail."""
    db = Database(f"sqlite:///{tmp_path / f'done_{label.lower()}.db'}")
    db.create_all()
    db, task_id = _make_db(tmp_path, f"done_{label.lower()}")
    with db.session_factory() as session:
        _walk_to_verifying(session, task_id)
        _verified(session, f"ev-{label.lower()}", task_id,
                  kind=EvidenceKind.EXECUTION_RESULT,
                  outcome=ExecutionOutcome.SUCCESS,
                  correlation_id="corr-done-op", source=source)
        r = TaskRegistry(session)
        with pytest.raises(TaskRegistryError, match="execution-authoritative source"):
            r.update_status(
                task_id, TaskState.DONE, actor="verifier",
                correlation_id="corr-done-op", evidence_refs=(f"ev-{label.lower()}",),
                verifier_capability=True,
            )


def test_done_refused_with_approval_evidence_only(tmp_path: Path) -> None:
    """Owner round 3: approval evidence authorizes START, never DONE."""
    from antigona.models import Approval

    db = Database(f"sqlite:///{tmp_path / 'done_approval_only.db'}")
    db.create_all()
    db, task_id = _make_db(tmp_path, "done_approval_only")
    with db.session_factory() as session:
        _walk_to_verifying(session, task_id)
        approval = Approval(
            id="approval-done-x", task_id=task_id, tool_name="x",
            arguments={}, risk_level="SAFE", reason="ok", decision="APPROVED",
        )
        session.add(approval)
        session.flush()
        _verified(session, "ev-approval-only", task_id,
                  kind=EvidenceKind.APPROVAL_DECISION,
                  source=EvidenceSource.OWNER_INPUT,
                  claim="approval:approval-done-x")
        r = TaskRegistry(session)
        with pytest.raises(TaskRegistryError, match="evidence kind EXECUTION_RESULT"):
            r.update_status(
                task_id, TaskState.DONE, actor="verifier",
                correlation_id="corr-done-op", evidence_refs=("ev-approval-only",),
                verifier_capability=True,
            )


def test_done_refused_with_execution_result_of_another_task(tmp_path: Path) -> None:
    db = Database(f"sqlite:///{tmp_path / 'done_foreign_result.db'}")
    db.create_all()
    db, task_id = _make_db(tmp_path, "done_foreign_result")
    with db.session_factory() as session:
        _walk_to_verifying(session, task_id)
        _verified(session, "ev-foreign-result", "other-task-id",
                  kind=EvidenceKind.EXECUTION_RESULT,
                  outcome=ExecutionOutcome.SUCCESS, correlation_id="corr-done-op")
        r = TaskRegistry(session)
        with pytest.raises(TaskRegistryError, match="unknown or belong to another task"):
            r.update_status(
                task_id, TaskState.DONE, actor="verifier",
                correlation_id="corr-done-op", evidence_refs=("ev-foreign-result",),
                verifier_capability=True,
            )


def test_done_refused_with_execution_result_of_other_operation(tmp_path: Path) -> None:
    db = Database(f"sqlite:///{tmp_path / 'done_other_op.db'}")
    db.create_all()
    db, task_id = _make_db(tmp_path, "done_other_op")
    with db.session_factory() as session:
        _walk_to_verifying(session, task_id)
        _verified(session, "ev-other-op", task_id,
                  kind=EvidenceKind.EXECUTION_RESULT,
                  outcome=ExecutionOutcome.SUCCESS, correlation_id="corr-OTHER-op")
        r = TaskRegistry(session)
        with pytest.raises(TaskRegistryError, match="does not match operation correlation"):
            r.update_status(
                task_id, TaskState.DONE, actor="verifier",
                correlation_id="corr-done-op", evidence_refs=("ev-other-op",),
                verifier_capability=True,
            )


@pytest.mark.parametrize(
    "label,outcome",
    [
        ("failed", ExecutionOutcome.FAILED),
        ("unavailable", ExecutionOutcome.UNAVAILABLE),
        ("partial", ExecutionOutcome.PARTIAL),
    ],
)
def test_done_refused_with_non_terminal_success_outcome(
    tmp_path: Path, label: str, outcome: ExecutionOutcome
) -> None:
    db = Database(f"sqlite:///{tmp_path / f'done_{label}.db'}")
    db.create_all()
    db, task_id = _make_db(tmp_path, f"done_{label}")
    with db.session_factory() as session:
        _walk_to_verifying(session, task_id)
        _verified(session, f"ev-{label}", task_id,
                  kind=EvidenceKind.EXECUTION_RESULT,
                  outcome=outcome, correlation_id="corr-done-op")
        r = TaskRegistry(session)
        with pytest.raises(TaskRegistryError, match="terminal SUCCESS outcome"):
            r.update_status(
                task_id, TaskState.DONE, actor="verifier",
                correlation_id="corr-done-op", evidence_refs=(f"ev-{label}",),
                verifier_capability=True,
            )


def test_done_accepted_with_correct_verified_execution_result(tmp_path: Path) -> None:
    """Owner round 3 positive: VERIFYING -> DONE with verifier capability and a
    correctly bound VERIFIED successful EXECUTION_RESULT is accepted."""
    db = Database(f"sqlite:///{tmp_path / 'done_positive.db'}")
    db.create_all()
    db, task_id = _make_db(tmp_path, "done_positive")
    with db.session_factory() as session:
        _walk_to_verifying(session, task_id)
        r = TaskRegistry(session)
        rec = r.update_status(
            task_id, TaskState.DONE, actor="verifier",
            correlation_id="corr-done-op", evidence_refs=("ev-exec",),
            verifier_capability=True,
        )
        assert rec.status is TaskState.DONE
