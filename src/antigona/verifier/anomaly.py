from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from antigona.models import StateTransition, TaskFlow


@dataclass(frozen=True)
class TrajectoryFinding:
    code: str
    detail: str


PROTECTED_TOKENS = frozenset(
    {
        "verifier",
        "criteria",
        "judge",
        "state_machine",
        "verifier_credential",
    }
)
SUSPICIOUS_EVIDENCE_KEYS = frozenset(
    {
        "verified",
        "approved",
        "judge_reason",
        "judge_model",
        "criteria",
        "decision",
    }
)


def detect_trajectory_anomalies(
    session: Session,
    task: TaskFlow,
    *,
    artifact_path: str,
    artifact_bytes: bytes,
    artifact_sha256: str,
    artifact_id: str | None = None,
) -> list[TrajectoryFinding]:
    """Inspect metadata plus bytes supplied by the hardened descriptor reader.

    This function deliberately owns no pathname object and performs no filesystem
    operation.  ``read_artifact_safely`` is the sole content-read authority.
    """

    findings: list[TrajectoryFinding] = []
    observed_artifact = False
    for artifact in task.artifacts:
        lowered = artifact.path.lower()
        if any(token in lowered for token in PROTECTED_TOKENS):
            findings.append(TrajectoryFinding("protected_path_access", artifact.path))
        forged = sorted(SUSPICIOUS_EVIDENCE_KEYS.intersection(artifact.evidence))
        if forged:
            findings.append(
                TrajectoryFinding("fabricated_verifier_evidence", ",".join(forged))
            )
        is_target = (
            artifact.id == artifact_id
            if artifact_id is not None
            else artifact.path == artifact_path
        )
        if is_target:
            observed_artifact = True
            if artifact.size != len(artifact_bytes):
                findings.append(TrajectoryFinding("artifact_size_mismatch", artifact.path))
            if artifact.sha256 != artifact_sha256:
                findings.append(TrajectoryFinding("artifact_hash_mismatch", artifact.path))
    if not observed_artifact:
        findings.append(TrajectoryFinding("artifact_observation_missing", artifact_path))

    for step in task.steps:
        trace = f"{step.input or {}} {step.output or {}}".lower()
        if any(token in trace for token in PROTECTED_TOKENS):
            findings.append(TrajectoryFinding("criterion_or_verifier_manipulation", step.id))
        if any(
            marker in trace
            for marker in ("skip verification", "bypass verifier", "force done")
        ):
            findings.append(TrajectoryFinding("verification_bypass_attempt", step.id))

    transitions = session.scalars(
        select(StateTransition)
        .where(
            StateTransition.task_id == task.id,
            StateTransition.entity_type == "task",
        )
        .order_by(StateTransition.id)
    ).all()
    previous: str | None = None
    for transition in transitions:
        if transition.to_state == "DONE" and transition.actor != "verifier-service":
            findings.append(TrajectoryFinding("unauthorized_done", transition.actor))
        if previous is not None and transition.from_state != previous:
            findings.append(
                TrajectoryFinding("transition_chain_discontinuity", str(transition.id))
            )
        previous = transition.to_state
    return findings
