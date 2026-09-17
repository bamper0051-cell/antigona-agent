"""PHASE 2 — WORKSPACE OWNERSHIP v2 · INV-02 Stable Repository Identity (repo_uuid).

Path-independent, durable, copy-dir-safe repository identity.

Invariants covered (see wave docs 02_REQUIREMENTS.md INV-02 / FR-1):
  * T-I1  same path + same repo  -> same repo_uuid across invocations (persisted).
  * T-I2  second clone of the SAME logical repo (same git origin) -> SAME repo_uuid.
  * T-I3  a different repository (different git origin) -> different repo_uuid.
  * T-I4  copy of the directory (same origin, different path) -> NOT a second
          active authority: either a new uuid or explicitly fenced
          non-authoritative. Chosen semantic here: SAME logical uuid but
          explicitly flagged authoritative=False (copy-dir safety).
  * T-I5  repo_uuid is NOT derived from the filesystem path (move/rename with a
          still-matching git origin keeps the same uuid).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from antigona.ownership.identity import (
    repo_uuid_for_workspace,
    resolve_repo_identity,
)

# Shared fake remote so two directories can reference the SAME logical origin.
SAME_ORIGIN = "https://github.com/example-owner/example-antigona.git"
OTHER_ORIGIN = "https://github.com/other-owner/other-repo.git"


def _git_init(root: Path, origin: str | None = None) -> None:
    """Initialise a throwaway git repo (optionally pointing at a fake origin)."""
    subprocess.run(["git", "-C", str(root), "init", "-q"], check=True)
    if origin is not None:
        subprocess.run(
            ["git", "-C", str(root), "config", "remote.origin.url", origin],
            check=True,
        )


def test_TI1_same_path_same_repo_stable_across_invocations(tmp_path: Path) -> None:
    repo = tmp_path / "work-a"
    repo.mkdir()
    _git_init(repo, SAME_ORIGIN)

    first = resolve_repo_identity(repo)
    second = resolve_repo_identity(repo)  # fresh call reads the persisted marker

    assert first.repo_uuid
    assert second.repo_uuid == first.repo_uuid
    # persisted marker actually exists on disk
    assert (repo / ".antigona-ownership" / "repo_uuid.json").is_file()


def test_TI2_second_clone_same_origin_same_repo_uuid(tmp_path: Path) -> None:
    repo_a = tmp_path / "clone-a"
    repo_b = tmp_path / "clone-b"  # second clone of the SAME logical repo
    repo_a.mkdir()
    repo_b.mkdir()
    _git_init(repo_a, SAME_ORIGIN)
    _git_init(repo_b, SAME_ORIGIN)

    uuid_a = repo_uuid_for_workspace(repo_a)
    uuid_b = repo_uuid_for_workspace(repo_b)

    assert uuid_a == uuid_b


def test_TI3_different_repo_different_repo_uuid(tmp_path: Path) -> None:
    repo_a = tmp_path / "repo-a"
    repo_b = tmp_path / "repo-b"
    repo_a.mkdir()
    repo_b.mkdir()
    _git_init(repo_a, SAME_ORIGIN)
    _git_init(repo_b, OTHER_ORIGIN)

    uuid_a = repo_uuid_for_workspace(repo_a)
    uuid_b = repo_uuid_for_workspace(repo_b)

    assert uuid_a != uuid_b


def test_TI4_copy_of_directory_is_not_second_active_authority(tmp_path: Path) -> None:
    repo = tmp_path / "original"
    repo.mkdir()
    _git_init(repo, SAME_ORIGIN)
    orig = resolve_repo_identity(repo)
    assert orig.authoritative is True

    # copy the whole directory (includes .git AND the ownership marker)
    copied = tmp_path / "copy"
    shutil.copytree(repo, copied)

    dup = resolve_repo_identity(copied)
    # Same logical repository (same git origin) -> same uuid is allowed, BUT it
    # must be explicitly fenced as non-authoritative: a copy is never a second
    # active authority.
    assert dup.repo_uuid == orig.repo_uuid
    assert dup.authoritative is False


def test_TI5_repo_uuid_not_derived_from_filesystem_path(tmp_path: Path) -> None:
    original = tmp_path / "project-root"
    original.mkdir()
    _git_init(original, SAME_ORIGIN)
    uuid_before = repo_uuid_for_workspace(original)

    # move/rename the whole directory; git origin still matches the marker
    renamed = tmp_path / "renamed-project"
    shutil.move(str(original), str(renamed))

    uuid_after = repo_uuid_for_workspace(Path(renamed))

    assert uuid_after == uuid_before
