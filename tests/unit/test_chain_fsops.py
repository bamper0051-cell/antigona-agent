"""Unit tests for the durability primitives (wave G1b, M1a/M1b).

Invariants under test (PLAN.md §4.3): F1 no durability claim without a sync, F2 no
overwrite of an immutable event, F3 no symlink/hardlink traversal, F4 failure is
classified and names the path.
"""

from __future__ import annotations

import errno
import os
import stat
from pathlib import Path

import pytest

from antigona.chain import fsops
from antigona.chain.errors import ChainPathError, ChainSyncError

# ── 7. write_atomic ──────────────────────────────────────────────────────────


def test_write_atomic_leaves_no_temp_file_and_replaces_atomically(tmp_path: Path) -> None:
    target = tmp_path / "created" / "HEAD"
    fsops.write_atomic(target, b"first\n", 0o644)
    assert target.read_bytes() == b"first\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o644

    fsops.write_atomic(target, b"second\n", 0o600)
    assert target.read_bytes() == b"second\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert sorted(entry.name for entry in target.parent.iterdir()) == ["HEAD"]


# ── 8. F2 — publication never overwrites ─────────────────────────────────────


def test_write_immutable_no_replace_raises_file_exists_and_does_not_overwrite(
    tmp_path: Path,
) -> None:
    final = tmp_path / "event.json"
    final.write_bytes(b"original")
    tmp = tmp_path / ".event-tmp"
    tmp.write_bytes(b"candidate")

    with pytest.raises(FileExistsError):
        fsops.write_immutable_no_replace(tmp, final)

    assert final.read_bytes() == b"original"

    other_final = tmp_path / "fresh.json"
    fsops.write_immutable_no_replace(tmp, other_final)
    assert other_final.read_bytes() == b"candidate"
    assert not tmp.exists()


# ── 9. EXDEV / hardlink-less filesystems fall back to O_EXCL ─────────────────


def test_write_immutable_no_replace_falls_back_to_o_excl_on_exdev(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _exdev(*args: object, **kwargs: object) -> None:
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr(os, "link", _exdev)

    tmp = tmp_path / ".event-tmp"
    tmp.write_bytes(b"payload")
    final = tmp_path / "event.json"
    fsops.write_immutable_no_replace(tmp, final)

    assert final.read_bytes() == b"payload"
    assert not tmp.exists()

    # The O_EXCL fallback must refuse to overwrite too.
    tmp2 = tmp_path / ".event-tmp2"
    tmp2.write_bytes(b"other")
    with pytest.raises(FileExistsError):
        fsops.write_immutable_no_replace(tmp2, final)
    assert final.read_bytes() == b"payload"


# ── 10. F1 — every created component's parent is synced ─────────────────────


def test_fsync_dir_is_called_for_the_parent_of_every_created_component(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    synced: list[Path] = []
    real = fsops.fsync_dir

    def _record(path: Path | str) -> None:
        synced.append(Path(path))
        real(path)

    monkeypatch.setattr(fsops, "fsync_dir", _record)
    base = tmp_path / "base"
    base.mkdir()
    fsops.mkdir_all_sync(base / "a" / "b" / "c")

    assert synced == [base, base / "a", base / "a" / "b"]
    assert (base / "a" / "b" / "c").is_dir()

    # An already-present tree is a no-op (race-free EEXIST outcome, no re-sync).
    synced.clear()
    fsops.mkdir_all_sync(base / "a" / "b" / "c")
    assert synced == []


# ── 11. F3 — no symlink / hardlink traversal ────────────────────────────────


def test_open_no_follow_refuses_symlink_and_hardlink(tmp_path: Path) -> None:
    target = tmp_path / "real.json"
    target.write_bytes(b"body")

    symlink = tmp_path / "sym.json"
    symlink.symlink_to(target)
    with pytest.raises(ChainPathError):
        fsops.open_no_follow(symlink, os.O_RDONLY)

    donor = tmp_path / "donor.json"
    donor.write_bytes(b"body")
    hardlink = tmp_path / "hard.json"
    os.link(donor, hardlink)
    for linked in (hardlink, donor):
        with pytest.raises(ChainPathError):
            fsops.open_no_follow(linked, os.O_RDONLY)

    # The genuine file still opens, and the fd is the caller's to close.
    fd = fsops.open_no_follow(target, os.O_RDONLY)
    try:
        assert os.read(fd, 4) == b"body"
    finally:
        os.close(fd)


# ── 12. F4 — a sync failure is typed and names the path ─────────────────────


def test_fsync_failure_raises_typed_error_naming_the_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(fd: int) -> None:
        raise OSError(errno.EIO, "Input/output error")

    # The directory is pre-created so the failing sync is the *file* sync, and the
    # refusal must name the file whose durability was not established (F4).
    target = tmp_path / "sub" / "HEAD"
    target.parent.mkdir()
    monkeypatch.setattr(os, "fsync", _boom)
    with pytest.raises(ChainSyncError) as excinfo:
        fsops.write_atomic(target, b"x\n", 0o644)
    assert excinfo.value.path == target
    assert str(target) in str(excinfo.value)
    assert not target.exists()
    assert [entry.name for entry in target.parent.iterdir()] == []
