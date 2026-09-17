"""Hermes RCA — storage integration tests (spec section 15).

Verifies the rca_error_records table is created on the shared Base metadata
(reusing existing storage, no separate DB) and that save/latest/by_correlation
round-trip correctly.
"""

from __future__ import annotations

from antigona.rca import ErrorEnvelope, RCAEngine
from antigona.rca.dedup import fingerprint
from antigona.rca.storage import RCARepository, get_repository


class TestRCARepository:
    """Storage round-trip on an isolated sqlite DB."""

    def _repo(self, tmp: str) -> tuple[RCARepository, str]:
        return get_repository(db_path=f"sqlite:///{tmp}/rca_test.db"), tmp

    def test_save_and_latest(self) -> None:
        import tempfile as _tf

        with _tf.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            repo, _ = self._repo(tmp)
            env = ErrorEnvelope.from_exception(
                RuntimeError("storage test failure"),
                source_component="worker",
                operation="op",
                correlation_id="cid-store",
            )
            result = RCAEngine().diagnose(env)
            repo.save(result, env, fingerprint(env), duplicate_count=3)
            latest = repo.latest(limit=5)
            assert latest, "expected at least one saved record"
            top = latest[0]
            assert top.rca_id == result.rca_id
            assert top.correlation_id == "cid-store"
            assert top.duplicate_count == 3
            assert top.category != "UNKNOWN"

    def test_by_correlation(self) -> None:
        import tempfile as _tf

        with _tf.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            repo, _ = self._repo(tmp)
            env = ErrorEnvelope.from_exception(
                RuntimeError("trace me"), source_component="mcp", operation="discovery",
                correlation_id="cid-trace",
            )
            result = RCAEngine().diagnose(env)
            repo.save(result, env, fingerprint(env), duplicate_count=1)
            rows = repo.by_correlation("cid-trace")
            assert len(rows) == 1
            assert rows[0].source_component == "mcp"
            # other correlation returns none
            assert repo.by_correlation("cid-nope") == []
