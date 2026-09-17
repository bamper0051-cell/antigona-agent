"""Unit tests for the content-addressed append-only chain (wave G1b, M1b).

Invariants under test (PLAN.md §4.4): I1 content address, I2 HEAD never outruns
durability, I3 CAS is mandatory, I4 stale predecessor is typed, I5 exact retry is
idempotent, I6 divergent payload under an identical digest fails closed, I7 the chain
walk is total and hostile-input safe, I10 HEAD is a single line.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import os
from contextlib import suppress
from pathlib import Path

import pytest

from antigona.chain import locking
from antigona.chain import store as store_module
from antigona.chain.errors import (
    ChainIntegrityError,
    InvalidSuccessorError,
    LegacyReadOnlyError,
    LockContendedError,
    StaleHeadError,
)
from antigona.chain.hashing import record_revision
from antigona.chain.records import RECORD_SCHEMA, ChainRecord
from antigona.chain.store import ChainStore


def _record(
    *,
    operation: str = "review/start",
    previous_revision: str = "",
    payload: dict[str, object] | None = None,
) -> ChainRecord:
    return ChainRecord(
        operation=operation,
        previous_revision=previous_revision,
        payload={"state": "reviewing"} if payload is None else payload,
    )


def _event_path(directory: Path, revision: str) -> Path:
    return directory / "events" / (revision[len("sha256:") :] + ".json")


# ── 13. genesis then successor ───────────────────────────────────────────────


def test_append_genesis_then_successor_moves_head_once(tmp_path: Path) -> None:
    store = ChainStore(tmp_path)
    assert store.read_head() == ""

    genesis = store.append("", _record())
    assert store.read_head() == genesis
    assert (tmp_path / "HEAD").read_bytes() == (genesis + "\n").encode()

    successor = store.append(genesis, _record(operation="review/finding"))
    assert successor != genesis
    assert store.read_head() == successor

    chain = store.load_chain(successor)
    assert chain.revisions == (genesis, successor)
    assert chain.genesis_revision == genesis
    assert chain.head_revision == successor
    assert chain.records[-1].previous_revision == genesis
    assert store.chain_identity() == chain.identity


# ── 14. I1 — the event file is named after its own digest ────────────────────


def test_event_file_is_named_after_its_own_digest(tmp_path: Path) -> None:
    store = ChainStore(tmp_path)
    revision = store.append("", _record())

    path = _event_path(tmp_path, revision)
    payload = path.read_bytes()
    assert hashlib.sha256(payload).hexdigest() == revision[len("sha256:") :]
    assert record_revision(payload) == revision
    assert payload.endswith(b"\n")

    document = json.loads(payload.decode())
    assert document["schema"] == RECORD_SCHEMA
    assert document["previous_revision"] == ""
    assert document["operation"] == "review/start"


# ── 15. I1 — identical records produce one file ──────────────────────────────


def test_identical_record_appends_only_one_event_file(tmp_path: Path) -> None:
    store = ChainStore(tmp_path)
    first = store.append("", _record())
    second = store.append("", _record())
    assert first == second
    assert sorted(entry.name for entry in (tmp_path / "events").iterdir()) == [
        first[len("sha256:") :] + ".json"
    ]
    assert store.read_head() == first


# ── 16. I2 — HEAD is written after the events directory is synced ────────────


def test_head_is_written_after_events_dir_sync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, str]] = []
    real_write_atomic = store_module.write_atomic
    real_fsync_dir = store_module.fsync_dir

    def _write_atomic(path: Path, payload: bytes, mode: int, **kwargs: object) -> None:
        calls.append(("write_atomic", Path(path).name))
        real_write_atomic(path, payload, mode, **kwargs)  # type: ignore[arg-type]

    def _fsync_dir(path: Path) -> None:
        calls.append(("fsync_dir", Path(path).name))
        real_fsync_dir(path)

    monkeypatch.setattr(store_module, "write_atomic", _write_atomic)
    monkeypatch.setattr(store_module, "fsync_dir", _fsync_dir)

    ChainStore(tmp_path).append("", _record())

    events_sync = calls.index(("fsync_dir", "events"))
    head_write = calls.index(("write_atomic", "HEAD"))
    assert events_sync < head_write


# ── 17. I2 — a failed publication leaves HEAD byte-identical ────────────────


def test_publish_failure_leaves_head_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ChainStore(tmp_path)
    genesis = store.append("", _record())
    head_before = (tmp_path / "HEAD").read_bytes()

    def _boom(tmp: Path, final: Path) -> None:
        raise OSError("simulated publication failure")

    monkeypatch.setattr(store_module, "write_immutable_no_replace", _boom)
    with pytest.raises(OSError):
        store.append(genesis, _record(operation="review/finding"))

    assert (tmp_path / "HEAD").read_bytes() == head_before
    assert store.read_head() == genesis
    assert sorted(entry.name for entry in (tmp_path / "events").iterdir()) == [
        genesis[len("sha256:") :] + ".json"
    ]


# ── 18. I3 — expected_revision is mandatory at the signature level ──────────


def test_append_without_expected_revision_is_a_type_error(tmp_path: Path) -> None:
    signature = inspect.signature(ChainStore.append)
    parameter = signature.parameters["expected_revision"]
    assert parameter.default is inspect.Parameter.empty
    chain_store = ChainStore(tmp_path)
    with pytest.raises(TypeError):
        chain_store.append(record=_record())  # type: ignore[call-arg]


# ── 19. I4 — stale predecessor carries the three revisions ──────────────────


def test_stale_predecessor_raises_stale_head_error_with_three_revisions(tmp_path: Path) -> None:
    store = ChainStore(tmp_path)
    genesis = store.append("", _record())
    wrong = "sha256:" + "0" * 64

    with pytest.raises(StaleHeadError) as excinfo:
        store.append(wrong, _record(operation="review/finding"))

    error = excinfo.value
    assert error.expected == wrong
    assert error.current == genesis
    assert error.candidate.startswith("sha256:")
    assert error.candidate not in (wrong, genesis)
    assert wrong in str(error)
    assert genesis in str(error)
    assert store.read_head() == genesis


# ── 20/21. I5 — exact retry is idempotent but never a shortcut ──────────────


def test_exact_retry_returns_same_revision_without_duplicate(tmp_path: Path) -> None:
    store = ChainStore(tmp_path)
    genesis = store.append("", _record())
    successor = store.append(genesis, _record(operation="review/finding"))
    files_before = sorted(entry.name for entry in (tmp_path / "events").iterdir())

    assert store.append(genesis, _record(operation="review/finding")) == successor
    assert sorted(entry.name for entry in (tmp_path / "events").iterdir()) == files_before
    assert store.read_head() == successor


def test_exact_retry_still_validates_the_whole_chain(tmp_path: Path) -> None:
    store = ChainStore(tmp_path)
    genesis = store.append("", _record())
    successor = store.append(genesis, _record(operation="review/finding"))

    # Corrupt an ancestor: the retry must walk the chain, not short-circuit.
    _event_path(tmp_path, genesis).write_bytes(b"{not json at all")
    with pytest.raises(ChainIntegrityError):
        store.append(genesis, _record(operation="review/finding"))
    assert store.read_head() == successor


# ── 22. I6 — same revision, divergent bytes → fail closed ───────────────────


def test_same_revision_divergent_bytes_raises_chain_integrity_error(tmp_path: Path) -> None:
    store = ChainStore(tmp_path)
    genesis = store.append("", _record())

    divergent = (
        json.dumps(
            {
                "schema": RECORD_SCHEMA,
                "operation": "review/start",
                "previous_revision": "",
                "payload": {"state": "tampered"},
            },
            sort_keys=True,
            indent=2,
        )
        + "\n"
    ).encode()
    _event_path(tmp_path, genesis).write_bytes(divergent)

    with pytest.raises(ChainIntegrityError):
        store.append("", _record())
    assert store.read_head() == genesis


# ── 23/24/25. I7 — the walk is total and hostile-input safe ─────────────────


def _write_event(directory: Path, document: dict[str, object]) -> str:
    payload = (json.dumps(document, sort_keys=True, indent=2) + "\n").encode()
    revision = record_revision(payload)
    path = directory / "events" / (revision[len("sha256:") :] + ".json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return revision


def test_load_chain_detects_cycle(tmp_path: Path) -> None:
    store = ChainStore(tmp_path)
    events = tmp_path / "events"
    events.mkdir()
    # Two records pointing at each other. The walk follows the *claimed*
    # predecessor link, so it must refuse to loop rather than recurse forever.
    first = "sha256:" + "11" * 32
    second = "sha256:" + "22" * 32
    for revision, previous in ((first, second), (second, first)):
        document = {
            "schema": RECORD_SCHEMA,
            "operation": "review/start",
            "previous_revision": previous,
            "payload": {"state": "reviewing"},
        }
        (events / (revision[len("sha256:") :] + ".json")).write_bytes(
            (json.dumps(document, sort_keys=True, indent=2) + "\n").encode()
        )

    with pytest.raises(ChainIntegrityError, match="cycle"):
        store.load_chain(first)


def test_load_chain_detects_discontinuous_predecessor(tmp_path: Path) -> None:
    store = ChainStore(tmp_path)
    (tmp_path / "events").mkdir()
    # A predecessor file whose *content* hashes to a different revision than the
    # link that names it: the recorded link is therefore discontinuous.
    claimed = "sha256:" + "ab" * 32
    _write_event(
        tmp_path,
        {
            "schema": RECORD_SCHEMA,
            "operation": "review/start",
            "previous_revision": "",
            "payload": {"state": "reviewing"},
        },
    )
    head = _write_event(
        tmp_path,
        {
            "schema": RECORD_SCHEMA,
            "operation": "review/finding",
            "previous_revision": claimed,
            "payload": {"state": "reviewing"},
        },
    )
    stale_payload = (
        json.dumps(
            {
                "schema": RECORD_SCHEMA,
                "operation": "review/start",
                "previous_revision": "",
                "payload": {"state": "reviewing"},
            },
            sort_keys=True,
            indent=2,
        )
        + "\n"
    ).encode()
    (tmp_path / "events" / (claimed[len("sha256:") :] + ".json")).write_bytes(stale_payload)

    with pytest.raises(ChainIntegrityError, match="discontinuous"):
        store.load_chain(head)


def test_load_chain_requires_exactly_one_genesis(tmp_path: Path) -> None:
    def _genesis_only(record: ChainRecord) -> None:
        if record.operation != "review/start":
            raise InvalidSuccessorError("first event must be review/start")

    # A chain whose tail is not a legal genesis is refused on the walk...
    permissive = ChainStore(tmp_path / "bad")
    bad_genesis = permissive.append("", _record(operation="review/finding"))
    strict = ChainStore(tmp_path / "bad", validate_genesis=_genesis_only)
    with pytest.raises(InvalidSuccessorError):
        strict.load_chain(bad_genesis)

    # ...and never created in the first place by a store that owns the rule.
    fresh = ChainStore(tmp_path / "guarded", validate_genesis=_genesis_only)
    with pytest.raises(InvalidSuccessorError):
        fresh.append("", _record(operation="review/finding"))
    assert fresh.read_head() == ""

    # A chain whose tail IS a legal genesis still loads, successor and all.
    good = ChainStore(tmp_path / "good")
    genesis = good.append("", _record())
    successor = good.append(genesis, _record(operation="review/finding"))
    validated = ChainStore(tmp_path / "good", validate_genesis=_genesis_only).load_chain(successor)
    assert validated.genesis_revision == genesis


# ── 26. I10 — HEAD is a single line ─────────────────────────────────────────


def test_read_head_absent_is_empty_string_and_invalid_content_raises(tmp_path: Path) -> None:
    store = ChainStore(tmp_path)
    assert store.read_head() == ""

    (tmp_path / "HEAD").write_text("not-a-revision\n")
    with pytest.raises(ChainIntegrityError):
        store.read_head()

    (tmp_path / "HEAD").write_text("sha256:" + "0" * 64 + "\n")
    assert store.read_head() == "sha256:" + "0" * 64

    # A HEAD file that EXISTS but holds no revision at all is unknown pointer
    # state, not an absent chain: only a missing file may answer "".  Reading a
    # blank file as "" lets the next genesis append move HEAD off a live lineage
    # without touching its events.
    for blank in (b"", b"\n", b"   \n", b"\t \n"):
        (tmp_path / "HEAD").write_bytes(blank)
        with pytest.raises(ChainIntegrityError):
            store.read_head()

    # The same rule with a real chain behind the pointer.
    other = tmp_path / "with-a-chain"
    chained = ChainStore(other)
    chained.append("", _record())
    for blank in (b"", b"\n", b"   \n"):
        (other / "HEAD").write_bytes(blank)
        with pytest.raises(ChainIntegrityError):
            chained.read_head()


def test_blank_head_file_never_reads_as_an_absent_chain(tmp_path: Path) -> None:
    """Fail closed: an unknown pointer state is not "proved empty".

    ``append`` writes HEAD atomically and always as ``<revision>\\n``, so a HEAD
    file holding nothing is corruption or interference.  Reading it as ``""``
    let ``append("", other_genesis)`` succeed and move HEAD onto a SECOND genesis
    while the first lineage's event file stayed on disk — a silent fork whose
    superseded records are never named again.
    """
    store = ChainStore(tmp_path)
    store.append("", _record())
    events_before = sorted(entry.name for entry in (tmp_path / "events").iterdir())
    assert len(events_before) == 1

    for blank in (b"", b"\n", b"   \n"):
        (tmp_path / "HEAD").write_bytes(blank)
        with pytest.raises(ChainIntegrityError):
            store.read_head()
        with pytest.raises(ChainIntegrityError):
            store.append("", _record(payload={"genesis": 2}))

    # Neither HEAD nor events/ moved: the refused appends changed nothing.
    assert (tmp_path / "HEAD").read_bytes() == b"   \n"
    assert sorted(entry.name for entry in (tmp_path / "events").iterdir()) == events_before
    # ...and the refused appends released the store lock on their way out.
    locking.acquire_store_lock(store.lock_path).release()


# ── 27. read-only store refuses to append ───────────────────────────────────


def test_read_only_store_refuses_append(tmp_path: Path) -> None:
    store = ChainStore(tmp_path, read_only=True, lineage_id="lin-1")
    with pytest.raises(LegacyReadOnlyError):
        store.append("", _record())
    assert not (tmp_path / "HEAD").exists()
    assert store.read_head() == ""


# ── 35. the head file's NAME is bound to its BYTES ──────────────────────────


def _canonical_bytes(
    *, operation: str, previous_revision: str, payload: dict[str, object]
) -> bytes:
    """Exactly what ``canonical_record_bytes`` would write for these fields."""
    return (
        json.dumps(
            {
                "schema": RECORD_SCHEMA,
                "operation": operation,
                "previous_revision": previous_revision,
                "payload": payload,
            },
            sort_keys=True,
            indent=2,
        )
        + "\n"
    ).encode()


def test_load_chain_refuses_head_whose_bytes_do_not_match_its_name(tmp_path: Path) -> None:
    """The head has no child to cross-check it, so the walk itself must.

    Every ancestor is bound to its own name by the child that names it, so a
    tampered ancestor is caught as a discontinuity.  The head has no such child:
    without an explicit name-vs-bytes comparison the head file's content can be
    swapped for any other well-formed record and the forged payload is served
    under the untampered revision.
    """
    store = ChainStore(tmp_path)
    genesis = store.append("", _record())
    head = store.append(
        genesis, _record(operation="review/verdict", payload={"verdict": "PASS"})
    )

    forged = _canonical_bytes(
        operation="review/verdict", previous_revision=genesis, payload={"verdict": "FAIL"}
    )
    recomputed = record_revision(forged)
    assert recomputed != head
    _event_path(tmp_path, head).write_bytes(forged)

    with pytest.raises(ChainIntegrityError) as excinfo:
        store.load_chain(head)

    message = str(excinfo.value)
    # Both values must be named: the revision that was requested and the
    # revision the bytes actually compute to.
    assert head in message
    assert recomputed in message


def test_load_chain_still_serves_an_untampered_head(tmp_path: Path) -> None:
    """Control for the check above: the honest head still loads, payload intact."""
    store = ChainStore(tmp_path)
    genesis = store.append("", _record())
    head = store.append(
        genesis, _record(operation="review/verdict", payload={"verdict": "PASS"})
    )

    chain = store.load_chain(head)
    assert chain.head_revision == head
    assert chain.revisions[-1] == head
    assert dict(chain.records[-1].payload) == {"verdict": "PASS"}
    # ...and an exact retry through the write path is still idempotent.
    assert store.append(genesis, _record(operation="review/verdict", payload={"verdict": "PASS"})) == head


# ── 36. a lock conflict must release the locks already taken ────────────────


def test_store_lock_conflict_releases_the_maintenance_lock(tmp_path: Path) -> None:
    """The maintenance lock is taken before the store lock.

    A ``LockContendedError`` from the store lock used to escape before the
    ``try``/``finally`` covering the guarded body: ``maintenance.release()``
    never ran, so the maintenance lock stayed held for the life of the process
    (and its file descriptor leaked), making the failed append block every later
    maintenance acquisition — including its own.
    """
    maintenance_path = tmp_path / "MAINTENANCE"
    store = ChainStore(tmp_path, maintenance_lock_path=maintenance_path)
    genesis = store.append("", _record())

    holder = locking.acquire_store_lock(store.lock_path)
    try:
        with pytest.raises(LockContendedError):
            store.append(genesis, _record(operation="review/finding"))
    finally:
        holder.release()

    # The store lock is free again and the maintenance lock must be too: a
    # second non-blocking acquisition of a still-held lock raises.
    reopened = locking.acquire_store_lock(maintenance_path)
    reopened.release()

    # ...and no descriptor for the maintenance lock was left open either.
    if Path("/proc/self/fd").is_dir():
        assert _open_descriptors(maintenance_path) == []


def _open_descriptors(path: Path) -> list[str]:
    """This process's open descriptors pointing at *path* (Linux ``/proc``)."""
    found: list[str] = []
    for entry in Path("/proc/self/fd").iterdir():
        with suppress(OSError):
            if os.readlink(entry) == str(path):
                found.append(entry.name)
    return found
