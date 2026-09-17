"""The C11 verdict journal: the frozen ChainStore wired to a real consumer (wave G1d).

Constitutional basis (ART-03, "no stubs / no fake capabilities"): ``antigona.chain``
was a frozen but *unconsumed* module — a declared, not-working capability.  The C11
deployment-manifest check (:func:`antigona.startup.validator.check_worktree_integrity`)
computes a CRITICAL verdict on every run and, before this wave, kept it nowhere: the
verdict existed only as a stdout line and an exit code, so nothing on disk could prove
that C11 was green at time T against envelope M, nor that the record was not rewritten.

Wiring invariants under test (PLAN §1.4):

* W1 — BOTH verdicts are journalled, red and green;
* W3 — the append is a compare-and-swap on HEAD with a bounded retry;
* W4 — a broken/unwritable journal NEVER changes the verdict, the ``critical`` filter
  or the exit code: the journal is a WARN side channel, never a gate;
* W6 — no secret and no path outside the evidence root reaches the record;
* W8 — ``paths.evidence_dir()`` failing (immutable deployment without a state root)
  is a WARN, not a traceback.
"""

from __future__ import annotations

import hashlib
import json
import socket
import sys
from pathlib import Path

import pytest

from antigona.chain import ChainRecord, ChainStore, record_revision
from antigona.core import paths
from antigona.startup import validator
from tests.unit.test_validator_c11 import _fixture

_C11_CHECK = "contract:C11:deployment_manifest"
_CHAIN_CHECK = "contract:C11:evidence_chain"
_CHAIN_RELPATH = Path("chain") / "c11"


def _evidence_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the chain root at ``tmp_path`` — never at the dev-default evidence root.

    Without this the journal would land in ``/var/lib/antigona/evidence`` (outside
    ``tmp_path``), which makes the suite depend on the environment and on run order:
    two tests appending on one shared HEAD race into ``StaleHeadError``.
    """
    evidence = tmp_path / "evidence"
    monkeypatch.setenv("ANTIGONA_EVIDENCE_DIR", str(evidence))
    return evidence


def _store(evidence: Path) -> ChainStore:
    return ChainStore(evidence / _CHAIN_RELPATH, lineage_id="c11-deployment-integrity")


def _last_payload(evidence: Path) -> dict[str, object]:
    store = _store(evidence)
    head = store.read_head()
    assert head != "", "no C11 verdict was journalled: HEAD is absent or empty"
    chain = store.load_chain(head)
    return dict(chain.records[-1].payload)


def _manifest_digest(manifest: Path) -> str:
    """Recompute the C11 anchor exactly as the check computes it (validator:215-219)."""
    data = json.loads(manifest.read_text(encoding="utf-8"))
    canonical = json.dumps(
        {k: v for k, v in data.items() if k not in {"report", "report_sha256"}},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(canonical).hexdigest()


def test_c11_verdict_is_appended_and_bound_to_the_manifest_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A green verdict is durable, and it carries the digest C11 authenticated."""
    evidence = _evidence_root(tmp_path, monkeypatch)
    manifest, _report = _fixture(tmp_path, monkeypatch)

    assert validator.run("manifest") == 0

    payload = _last_payload(evidence)
    assert payload["check"] == _C11_CHECK
    assert payload["ok"] is True
    assert payload["manifest_sha256"] == _manifest_digest(manifest)
    data = json.loads(manifest.read_text(encoding="utf-8"))
    assert payload["commit"] == data["commit"]
    assert payload["source_commit"] == data["source_commit"]
    assert payload["release_metadata_commit"] == data["release_metadata_commit"]
    assert payload["mode"] == "gitless"
    assert payload["file_count"] == 1
    # W2 — measured values, plus the human-readable detail as a comment only.
    assert isinstance(payload["detail"], str) and payload["detail"]
    assert isinstance(payload["recorded_at"], str) and payload["recorded_at"]
    # W6 — nothing secret-shaped, and every value is a canonical JSON scalar.
    assert not [key for key in payload if "token" in key or "pin" in key or "owner" in key]
    assert all(isinstance(value, (str, int, bool)) for value in payload.values())
    json.dumps(payload, sort_keys=True, allow_nan=False)


def test_a_failed_c11_verdict_is_journalled_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """W1 — a journal of successes only is the worst kind of journal."""
    evidence = _evidence_root(tmp_path, monkeypatch)
    _fixture(tmp_path, monkeypatch)
    (tmp_path / "app.py").write_text("changed")

    assert validator.run("manifest") == 1

    payload = _last_payload(evidence)
    assert payload["check"] == _C11_CHECK
    assert payload["ok"] is False
    assert payload["detail"]


def test_two_consecutive_runs_form_a_two_link_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """W3 — consecutive checks are ordered links, not independent files."""
    evidence = _evidence_root(tmp_path, monkeypatch)
    _fixture(tmp_path, monkeypatch)

    assert validator.run("manifest") == 0
    assert validator.run("manifest") == 0

    store = _store(evidence)
    chain = store.load_chain(store.read_head())
    assert len(chain.records) == 2
    assert chain.records[0].previous_revision == ""
    assert chain.records[1].previous_revision == chain.revisions[0]
    assert chain.revisions[0] != chain.revisions[1]
    assert store.read_head() == chain.revisions[1]
    assert store.chain_identity() == chain.identity


def test_identical_checks_are_distinct_links_and_never_rewrite_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A repeated check on an unchanged envelope is a NEW link, not a dedup.

    Deduplicating identical verdicts would hide the re-check, which is the one
    thing an audit journal must not do (PLAN §7 prohibition 20).
    """
    evidence = _evidence_root(tmp_path, monkeypatch)
    _fixture(tmp_path, monkeypatch)

    assert validator.run("manifest") == 0
    store = _store(evidence)
    first = store.read_head()
    first_bytes = store.event_path(first).read_bytes()
    first_listing = sorted(p.name for p in store.events_dir.iterdir())

    assert validator.run("manifest") == 0

    chain = store.load_chain(store.read_head())
    assert len(chain.records) == 2
    second = chain.revisions[1]
    assert second != first
    # History is append-only: the first event is byte-identical and still name-bound.
    assert store.event_path(first).read_bytes() == first_bytes
    assert record_revision(store.event_path(first).read_bytes()) == first
    listing = sorted(p.name for p in store.events_dir.iterdir())
    assert set(first_listing).issubset(set(listing))
    assert len(listing) == 2


def test_a_broken_chain_does_not_change_the_c11_verdict_nor_the_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """W4 — an unwritable journal is a WARN, never a verdict and never a brick."""
    evidence = _evidence_root(tmp_path, monkeypatch)
    _fixture(tmp_path, monkeypatch)
    # Occupy the chain root with a regular file, so the store cannot be created.
    (evidence / "chain").mkdir(parents=True)
    (evidence / _CHAIN_RELPATH).write_text("not a chain")

    assert validator.run("manifest") == 0

    res = validator.record_c11_verdict(validator.check_worktree_integrity())
    assert res.check == _CHAIN_CHECK
    assert res.severity == validator._SEV_WARN
    assert res.ok is False
    assert res.detail
    # The verdict itself is untouched by the failed journal write.
    assert validator.check_worktree_integrity().ok is True


def test_chain_root_resolution_failure_is_a_warning_not_a_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """W8 — an immutable deployment without a state root must not traceback."""
    _fixture(tmp_path, monkeypatch)
    for name in ("ANTIGONA_STATE_ROOT", "ANTIGONA_EVIDENCE_DIR", "ANTIGONA_EVIDENCE_ROOT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(paths, "is_immutable_deployment", lambda: True)

    with pytest.raises(RuntimeError):
        paths.evidence_dir()

    assert validator.run("manifest") == 0
    res = validator.record_c11_verdict(validator.check_worktree_integrity())
    assert res.check == _CHAIN_CHECK
    assert res.severity == validator._SEV_WARN
    assert res.ok is False
    assert res.detail


# ── B8: the journal attempt is guarded end to end (host, time, CAS, root) ─────

#: The exact payload contract of a C11 journal record: the five fields the journal
#: itself owns, plus the manifest anchors the C11 check already measured (W2/W6).
_JOURNAL_BASE_KEYS = frozenset({"check", "ok", "detail", "recorded_at", "host"})
_GREEN_ANCHOR_KEYS = frozenset(
    {
        "mode",
        "commit",
        "source_commit",
        "release_metadata_commit",
        "file_count",
        "manifest_sha256",
        "report_sha256",
    }
)


def _failing_hostname(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``socket.gethostname`` raise the way a broken/exotic host does (B8).

    ``record_c11_verdict`` imports ``socket`` at call time, so patching the module
    attribute is what its preamble actually calls.
    """

    def _raise(*_args: object, **_kwargs: object) -> str:
        raise OSError("forced gethostname fail")

    monkeypatch.setattr(socket, "gethostname", _raise)


def test_a_failing_hostname_is_a_warning_not_a_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """B8 — building the record is journal work: it may only ever WARN.

    ``socket.gethostname()`` / ``datetime.now()`` / the ``detail`` truncation live
    inside the guarded region, so a failure there yields the WARN
    ``contract:C11:evidence_chain`` entry (ok=False) and leaves the verdict, the
    report and the exit code exactly where the green envelope put them (W4/W8).
    """
    _evidence_root(tmp_path, monkeypatch)
    _fixture(tmp_path, monkeypatch)
    baseline = validator.run("manifest")
    capsys.readouterr()

    _failing_hostname(monkeypatch)

    rc = validator.run("manifest")  # must not raise (no traceback, no escape)
    captured = capsys.readouterr()
    res = validator.record_c11_verdict(validator.check_worktree_integrity())

    assert rc == baseline == 0, "a failing hostname changed the C11 verdict's exit code"
    assert res.check == _CHAIN_CHECK
    assert res.severity == validator._SEV_WARN
    assert res.ok is False
    assert res.detail.startswith("C11 verdict journal unavailable:")
    assert "forced gethostname fail" in res.detail
    # The report is still printed, with the CRITICAL verdict AND the WARN chain entry.
    assert _C11_CHECK in captured.out
    assert f"[CRITICAL] ✅ {_C11_CHECK}" in captured.out
    assert "❌ contract:C11:evidence_chain" in captured.out
    assert "КРИТИЧЕСКИЕ НАРУШЕНИЯ" not in captured.out
    assert "✅ Валидация пройдена." in captured.out
    assert "Traceback" not in captured.err


def test_main_keeps_its_exit_code_when_the_hostname_lookup_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """B8, at the entry point the deployment script actually calls.

    A HEALTHY candidate must not be failed by telemetry: ``main()`` used to let the
    ``OSError`` escape into its ``except Exception`` and return the fail-closed 1.
    """
    _evidence_root(tmp_path, monkeypatch)
    _fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(sys, "argv", ["validator", "--check=manifest"])
    baseline = validator.main()
    capsys.readouterr()

    _failing_hostname(monkeypatch)

    rc = validator.main()
    captured = capsys.readouterr()

    assert baseline == 0
    assert rc == baseline, "the fail-closed path swallowed a healthy deployment"
    assert _C11_CHECK in captured.out
    assert "❌ contract:C11:evidence_chain" in captured.out
    assert "✅ Валидация пройдена." in captured.out
    assert "fail-closed exit 1" not in captured.err
    assert "Traceback" not in captured.err


def test_the_payload_carries_exactly_the_contracted_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """W2/W6 — pin the set, not just the absence of a few secret-shaped names.

    Pinning only ``"token" not in key`` cannot notice an extra field being dropped
    into the record by a future edit; the exact key set can.
    """
    evidence = _evidence_root(tmp_path, monkeypatch)
    _fixture(tmp_path, monkeypatch)

    assert validator.run("manifest") == 0
    green = _last_payload(evidence)
    assert set(green) == _JOURNAL_BASE_KEYS | _GREEN_ANCHOR_KEYS

    # A red verdict carries no anchors, by construction: nothing was authenticated.
    (tmp_path / "app.py").write_text("changed")
    assert validator.run("manifest") == 1
    red = _last_payload(evidence)
    assert set(red) == _JOURNAL_BASE_KEYS
    assert red["ok"] is False


def test_a_lost_compare_and_swap_is_retried_and_journalled_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """W3 — a concurrent writer moving HEAD between read and append is survivable.

    The interlopers is appended through the REAL :meth:`ChainStore.append`, so the
    first attempt fails with a genuine ``StaleHeadError`` raised by the CAS, not with
    a simulated one: dropping the retry loop turns this test red (the call would
    return the "refused after 3 compare-and-swap attempts" WARN instead).
    """
    evidence = _evidence_root(tmp_path, monkeypatch)
    _fixture(tmp_path, monkeypatch)

    real_append = ChainStore.append
    attempts: list[str] = []

    def interloping_append(self: ChainStore, expected_revision: str, record: ChainRecord) -> str:
        attempts.append(expected_revision)
        if len(attempts) == 1:
            real_append(
                self,
                expected_revision,
                ChainRecord(operation="test/concurrent-writer", payload={"check": "intruder"}),
            )
        return real_append(self, expected_revision, record)

    monkeypatch.setattr(ChainStore, "append", interloping_append)

    res = validator.record_c11_verdict(validator.check_worktree_integrity())

    assert res.check == _CHAIN_CHECK
    assert res.ok is True, f"the lost CAS was not retried: {res.detail}"
    assert res.detail.startswith("C11 verdict journalled at")
    assert len(attempts) == 2, "the retry must re-derive HEAD and append exactly once more"

    store = _store(evidence)
    chain = store.load_chain(store.read_head())
    assert [r.operation for r in chain.records] == ["test/concurrent-writer", "c11/deployment-integrity"]
    assert chain.records[1].previous_revision == chain.revisions[0]
    assert store.read_head() == chain.revisions[1]
