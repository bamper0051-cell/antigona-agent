from __future__ import annotations

from pathlib import Path

import pytest

from antigona.core.evidence_registry import (
    EvidenceNotFound,
    EvidenceNotTrusted,
    EvidenceRecord,
    EvidenceRegistry,
    EvidenceSource,
    EvidenceStatus,
    InvalidEvidenceTransition,
)
from antigona.database import Database


def _registry(tmp_path: Path) -> EvidenceRegistry:
    db = Database(f"sqlite:///{tmp_path / 'evidence_registry.db'}")
    db.create_all()
    session = db.session_factory()
    return EvidenceRegistry(session)


def test_lifecycle_proposed_observed_verified(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    proposed = registry.register(
        evidence_id="ev-1",
        task_id="task-1",
        attempt_id="attempt-1",
        type="test_result",
        source=EvidenceSource.TEST_RUNNER,
        supports_claims=("execution.success",),
    )
    observed = registry.observe("ev-1", "tests://run/1", "a" * 64)
    verified = registry.verify("ev-1", verified_by="verifier")

    assert proposed.status is EvidenceStatus.PROPOSED
    assert observed.status is EvidenceStatus.OBSERVED
    assert verified.status is EvidenceStatus.VERIFIED
    assert verified.verified_by == "verifier"


def test_reject_paths(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    registry.register(
        evidence_id="ev-reject-1",
        task_id="task-1",
        attempt_id="attempt-1",
        type="source_snapshot",
        source=EvidenceSource.SOURCE_CODE,
    )
    rejected = registry.reject("ev-reject-1", "invalid artifact")
    assert rejected.status is EvidenceStatus.REJECTED

    registry.register(
        evidence_id="ev-reject-2",
        task_id="task-1",
        attempt_id="attempt-2",
        type="runtime_probe",
        source=EvidenceSource.RUNTIME_PROBE,
    )
    registry.observe("ev-reject-2", "runtime://probe/2", "b" * 64)
    rejected_observed = registry.reject("ev-reject-2", "probe mismatch")
    assert rejected_observed.status is EvidenceStatus.REJECTED


def test_model_output_can_never_be_verified(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    registry.register(
        evidence_id="ev-model",
        task_id="task-2",
        attempt_id="attempt-1",
        type="plan",
        source=EvidenceSource.MODEL_OUTPUT,
    )
    registry.observe("ev-model", "model://plan/1", "c" * 64)

    with pytest.raises(InvalidEvidenceTransition):
        registry.verify("ev-model", verified_by="verifier")


def test_is_trusted_and_assert_trusted(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    registry.register(
        evidence_id="ev-trust",
        task_id="task-3",
        attempt_id="attempt-1",
        type="git_ref",
        source=EvidenceSource.GIT,
    )

    assert not registry.is_trusted("ev-trust")
    assert not registry.is_trusted("missing")

    with pytest.raises(EvidenceNotTrusted):
        registry.assert_trusted("ev-trust")

    with pytest.raises(EvidenceNotTrusted):
        registry.assert_trusted("missing")

    registry.observe("ev-trust", "git://sha/1", "d" * 64)
    registry.verify("ev-trust", verified_by="verifier")

    assert registry.is_trusted("ev-trust")
    registry.assert_trusted("ev-trust")


def test_fail_closed_for_unknown_evidence_id(tmp_path: Path) -> None:
    registry = _registry(tmp_path)

    with pytest.raises(EvidenceNotFound):
        registry.get("missing")

    with pytest.raises(EvidenceNotFound):
        registry.observe("missing", "artifact://x", "f" * 64)


def test_for_task_returns_ordered_records(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    registry.register(
        evidence_id="ev-task-1",
        task_id="task-order",
        attempt_id="attempt-1",
        type="source",
        source=EvidenceSource.SOURCE_CODE,
    )
    registry.register(
        evidence_id="ev-task-2",
        task_id="task-order",
        attempt_id="attempt-2",
        type="test",
        source=EvidenceSource.TEST_RUNNER,
    )
    registry.register(
        evidence_id="ev-other",
        task_id="task-other",
        attempt_id="attempt-1",
        type="other",
        source=EvidenceSource.OWNER_INPUT,
    )

    records = registry.for_task("task-order")

    assert isinstance(records[0], EvidenceRecord)
    assert [record.evidence_id for record in records] == ["ev-task-1", "ev-task-2"]


def test_proposed_cannot_jump_directly_to_verified(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    registry.register(
        evidence_id="ev-direct",
        task_id="task-direct",
        attempt_id="attempt-1",
        type="raw",
        source=EvidenceSource.OWNER_INPUT,
    )

    with pytest.raises(InvalidEvidenceTransition):
        registry.verify("ev-direct", verified_by="verifier")

