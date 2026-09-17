from __future__ import annotations

from pathlib import Path

from antigona.core.evidence_registry import EvidenceRegistry, EvidenceSource, EvidenceStatus
from antigona.database import Database


def test_evidence_registry_persistence_round_trip_sqlite(tmp_path: Path) -> None:
    db = Database(f"sqlite:///{tmp_path / 'evidence_registry_roundtrip.db'}")
    db.create_all()

    with db.session_factory() as session:
        registry = EvidenceRegistry(session)
        registry.register(
            evidence_id="ev-roundtrip",
            task_id="task-rt",
            attempt_id="attempt-1",
            type="test_result",
            source=EvidenceSource.TEST_RUNNER,
            supports_claims=("execution.success", "artifact.hash"),
        )
        registry.observe("ev-roundtrip", "tests://run/42", "e" * 64)
        registry.verify("ev-roundtrip", verified_by="verifier-service")
        session.commit()

    with db.session_factory() as session:
        registry = EvidenceRegistry(session)
        record = registry.get("ev-roundtrip")

        assert record.status is EvidenceStatus.VERIFIED
        assert record.sha256 == "e" * 64
        assert record.artifact_reference == "tests://run/42"
        assert record.supports_claims == ("execution.success", "artifact.hash")
        assert record.verified_by == "verifier-service"
        assert registry.is_trusted("ev-roundtrip")

