"""P1-FENCE OS-identity layer: vectors no string comparison can catch.

The lexical matrix in :mod:`tests.security.test_workspace_fence` closes the
syntax vectors (``..``, absolute, symlink component, normalization, encoded).
This module proves the SECOND layer wired into the same chokepoints — the
kernel's own answer (``st_dev`` + ``st_ino``) after walking a path up to its
deepest existing ancestor — and that it closes the classes lexical checks
cannot: the sibling-prefix trap, a symlinked ancestor that leaves the root,
Unicode NFC/NFD equivalence, a mount/bind alias of the same inode, a
not-yet-created target under a real root, and a root that has no OS identity
at all (must fail closed with a typed error, never ``True``).

Two defects found by independent review are pinned here as their own vector
classes, each exercised at BOTH chokepoints:

* an UNRESOLVED path with a symlink component (``ws/link/secret.txt``) answered
  ``True`` — the layer was only correct for input the caller had already
  resolved;
* a mount point inside the root reaching another DEVICE (``/dev/shm`` vs
  ``/tmp``) was allowed, because ``is_symlink()`` is ``False`` for a mount
  point and ``resolve()`` does not cross it.

Both use REAL kernel fixtures (``mount --bind``), never an ``os.stat`` mock,
and skip with an explicit reason only where the environment forbids mounts.
The same-device bind residue is pinned by a test that documents the limit.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import unicodedata
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from antigona import path_boundary
from antigona.filesystem import WorkspaceViolation, validate_relative_path
from antigona.path_boundary import (
    PathIdentityError,
    RootIdentityError,
    deepest_existing_ancestor,
    is_contained_by_root_identity,
    same_directory_identity,
)
from antigona.worker.tools import ToolError, WorkspaceGuard

#: Both canonical guard chokepoints, exercised identically.
GUARDS: list[tuple[str, type[ValueError], Callable[[Path, str], Path]]] = [
    (
        "filesystem",
        WorkspaceViolation,
        lambda workspace, path: _filesystem_guard(workspace, path),
    ),
    ("worker", ToolError, lambda workspace, path: WorkspaceGuard(workspace).resolve(path)),
]


def _filesystem_guard(workspace: Path, path: str) -> Path:
    validate_relative_path(workspace, path)
    return workspace / path


# ── Real kernel fixtures (no os.stat mocks) ──────────────────────────────────


@contextmanager
def bind_mount(source: Path, target: Path) -> Iterator[None]:
    """Bind-mount *source* onto *target* with the REAL kernel, then unmount.

    ``os.stat`` is never patched: the device boundary and the shared-inode alias
    are produced by ``mount --bind``, so the layer is judged by an answer the
    operating system actually gives.  Where the environment forbids mounts
    (no ``mount``/``umount``, or no ``CAP_SYS_ADMIN``) the test skips with the
    kernel's own reason instead of silently weakening the assertion.
    """
    mount_binary = shutil.which("mount")
    umount_binary = shutil.which("umount")
    if os.name != "posix" or mount_binary is None or umount_binary is None:
        pytest.skip("bind mounts unavailable: POSIX mount/umount required")
    mounted = subprocess.run(
        [mount_binary, "--bind", str(source), str(target)],
        capture_output=True,
        text=True,
        check=False,
    )
    if mounted.returncode != 0:
        pytest.skip(
            "bind mount refused by the kernel (needs CAP_SYS_ADMIN): "
            f"{mounted.stderr.strip()}"
        )
    try:
        yield
    finally:
        for unmount_argv in ([umount_binary, str(target)], [umount_binary, "-l", str(target)]):
            if subprocess.run(unmount_argv, capture_output=True, text=True, check=False).returncode == 0:
                break


def _is_mounted(target: Path) -> bool:
    """Kernel-level mount check — ``/proc/self/mountinfo``, not ``ismount``.

    ``os.path.ismount`` finishes by comparing ``realpath`` values and therefore
    reports ``False`` for a SAME-device bind mount (``realpath`` does not cross
    mounts); the kernel's own mount table is authoritative for both the
    cross-device and the same-device case.
    """
    mountinfo = Path("/proc/self/mountinfo")
    if not mountinfo.exists():
        return os.path.ismount(target)
    resolved = os.path.realpath(target)
    for line in mountinfo.read_text(encoding="utf-8", errors="replace").splitlines():
        fields = line.split()
        if len(fields) > 4 and os.path.realpath(fields[4].replace("\\040", " ")) == resolved:
            return True
    return False


@pytest.fixture
def foreign_device_dir() -> Iterator[Path]:
    """A real directory on a DIFFERENT device than ``tmp_path`` (``/dev/shm``).

    ``/dev/shm`` is a tmpfs on its own device while ``tmp_path`` lives on the
    ``/tmp`` volume, so mounting one onto the other creates a genuine device
    boundary inside the root without patching ``os.stat``.
    """
    base = Path("/dev/shm")
    if not base.is_dir():
        pytest.skip("no foreign-filesystem directory available (expected /dev/shm)")
    directory = base / f"antigona-fence-{os.getpid()}-{uuid.uuid4().hex}"
    directory.mkdir()
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


# ── Typed, fail-closed root identity ─────────────────────────────────────────


def test_identity_error_types_are_typed_and_distinct() -> None:
    assert issubclass(PathIdentityError, ValueError)
    assert issubclass(RootIdentityError, PathIdentityError)
    assert RootIdentityError is not PathIdentityError


def test_missing_root_never_returns_true(tmp_path: Path) -> None:
    missing = tmp_path / "no-such-root"

    for target in (missing / "x", missing, missing / "a" / "b"):
        with pytest.raises(RootIdentityError):
            is_contained_by_root_identity(missing, target)

    # A root that is a regular file has no directory identity either.
    file_root = tmp_path / "root.txt"
    file_root.write_text("not a directory", encoding="utf-8")
    with pytest.raises(RootIdentityError):
        is_contained_by_root_identity(file_root, file_root)

    # And same_directory never claims identity for a path that has none.
    assert same_directory_identity(missing, missing) is False
    assert same_directory_identity(file_root, file_root) is False


@pytest.mark.parametrize(("guard_name", "error", "guard"), GUARDS, ids=[row[0] for row in GUARDS])
def test_missing_root_fails_closed_at_every_chokepoint(
    tmp_path: Path,
    guard_name: str,
    error: type[ValueError],
    guard: Callable[[Path, str], Path],
) -> None:
    """A non-existent workspace root must DENY, not act as an open boundary.

    Previously a root that did not exist still passed ``relative_to`` against
    itself, so *any* relative name was accepted; the identity layer refuses.

    ``WorkspaceGuard`` materialises its root at construction, so its missing-root
    answer is exercised after the root is removed underneath it (the same code
    path a race takes); ``validate_relative_path`` takes the root as given.
    """
    workspace = tmp_path / "does-not-exist-yet"
    if guard_name == "worker":
        worker_guard = WorkspaceGuard(workspace)
        assert worker_guard.resolve("warm-up.txt").name == "warm-up.txt"
        assert workspace.is_dir()
        workspace.rmdir()
        with pytest.raises(error):
            worker_guard.resolve("some/relative.txt")
        return
    with pytest.raises(error):
        guard(workspace, "some/relative.txt")


def test_deepest_existing_ancestor_walks_up_to_the_real_root(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    deep = root / "a" / "b" / "c" / "d.txt"
    assert deepest_existing_ancestor(str(deep)) == str(root)
    assert deepest_existing_ancestor(str(root)) == str(root)
    # A missing child is answered by the deepest ancestor that does exist.
    assert deepest_existing_ancestor(str(tmp_path / "gone" / "x")) == str(tmp_path)


# ── Vector 1: sibling-prefix trap ────────────────────────────────────────────


def test_sibling_prefix_trap_is_not_contained(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    sibling = tmp_path / "ws-sibling"
    sibling.mkdir()
    (sibling / "secret.txt").write_text("classified", encoding="utf-8")

    # The trap that defeats a naive prefix comparison: the sibling's path IS a
    # string prefix match for the root.
    assert str(sibling).startswith(str(root))

    # Identity is not fooled — a different directory is a different inode.
    assert same_directory_identity(root, sibling) is False
    assert is_contained_by_root_identity(root, sibling) is False
    assert is_contained_by_root_identity(root, sibling / "secret.txt") is False

    # ...while a genuine child of the root is contained.
    (root / "secret.txt").write_text("in bounds", encoding="utf-8")
    assert is_contained_by_root_identity(root, root / "secret.txt") is True


# ── Vector 2: symlinked ancestor escaping the root ───────────────────────────


def test_identity_rejects_symlinked_ancestor_escape(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("classified", encoding="utf-8")

    (root / "link").symlink_to(outside, target_is_directory=True)
    escaped = (root / "link" / "secret.txt").resolve(strict=False)
    assert escaped == outside / "secret.txt"
    assert is_contained_by_root_identity(root, escaped) is False

    # Tie-breaking finding (derived from the Go original): a symlink that points
    # INSIDE the root resolves to a directory with the root's own identity
    # chain, so the identity layer accepts it.  It is the guard's separate
    # symlink-component rule — not the identity layer — that refuses it.
    (root / "real").mkdir()
    (root / "inside_link").symlink_to(root / "real", target_is_directory=True)
    inside = (root / "inside_link" / "f.txt").resolve(strict=False)
    assert is_contained_by_root_identity(root, inside) is True


@pytest.mark.parametrize(("guard_name", "error", "guard"), GUARDS, ids=[row[0] for row in GUARDS])
def test_symlinked_ancestor_escape_denied_by_both_layers(
    tmp_path: Path,
    guard_name: str,
    error: type[ValueError],
    guard: Callable[[Path, str], Path],
) -> None:
    del guard_name
    root = tmp_path / "ws"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "link").symlink_to(outside, target_is_directory=True)

    with pytest.raises(error):
        guard(root, "link/secret.txt")
    # The identity layer independently reaches the same verdict on the
    # resolved escape target, so removing either layer still denies.
    assert is_contained_by_root_identity(root, outside / "secret.txt") is False
    # ...and on the UNRESOLVED spelling the caller never resolved: the layer
    # used to answer True here (only the lexical parent `ws` was compared with
    # the root), which is the independence defect this vector pins.
    assert is_contained_by_root_identity(root, root / "link" / "secret.txt") is False
    assert is_contained_by_root_identity(root, root / "link") is False


# ── Vector 3: Unicode NFC vs NFD ─────────────────────────────────────────────


def test_identity_uses_lookup_not_unicode_string_normalization(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    nfc_dir = root / "caf\u00e9"       # NFC: e + combining acute (single code point)
    nfd_dir = root / "cafe\u0301"      # NFD: e + U+0301
    nfc_dir.mkdir()

    # Canonically equivalent spellings — string/NFKC comparison cannot tell them
    # apart, which is exactly the defect this layer removes.
    assert unicodedata.normalize("NFC", nfc_dir.name) == unicodedata.normalize("NFC", nfd_dir.name)
    assert nfc_dir.name != nfd_dir.name

    normalising_volume = nfd_dir.exists()
    os_says_same = normalising_volume and os.path.samefile(nfc_dir, nfd_dir)
    assert same_directory_identity(nfc_dir, nfd_dir) is os_says_same
    if not normalising_volume:
        # On a non-normalising volume (ext4/tmpfs) the NFD spelling is a
        # DIFFERENT directory, and identity correctly refuses to conflate them.
        assert same_directory_identity(nfc_dir, nfd_dir) is False

    # A not-yet-created equivalent spelling under a real root is still contained
    # (answered by its deepest existing ancestor).
    assert is_contained_by_root_identity(root, nfd_dir / "new.txt") is True

    # An equivalent spelling OUTSIDE the root is never contained.
    outside_nfd = tmp_path / "cafe\u0301"
    outside_nfd.mkdir()
    assert is_contained_by_root_identity(root, outside_nfd / "f.txt") is False


# ── Vector 4: mount / symlink alias to the same inode ────────────────────────


def test_symlink_alias_of_the_same_inode_is_one_identity(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (real / "sub").mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)

    # Two spellings, one directory — decided by the OS, not the string.
    assert same_directory_identity(real, alias) is True
    assert is_contained_by_root_identity(real, alias / "sub" / "new.txt") is True
    assert is_contained_by_root_identity(alias, real / "sub") is True

    # A distinct directory is never the same identity, even on one device.
    other = tmp_path / "other"
    other.mkdir()
    root_stat = os.stat(real)
    other_stat = os.stat(other)
    assert root_stat.st_dev == other_stat.st_dev
    assert (root_stat.st_dev, root_stat.st_ino) != (other_stat.st_dev, other_stat.st_ino)
    assert is_contained_by_root_identity(real, other / "x") is False


def test_bind_mount_alias_to_the_same_inode_is_contained(tmp_path: Path) -> None:
    """A REAL bind mount, not a mocked ``os.stat``: one directory, two names.

    ``mount --bind`` makes ``alias`` the same directory as ``real`` — the kernel
    itself reports one ``(st_dev, st_ino)`` for both spellings, so the identity
    layer must accept the alias.  Proof that the decision is the operating
    system's identity and not a path string, with no mock in the way.
    """
    real = tmp_path / "real"
    real.mkdir()
    (real / "inside.txt").write_text("in bounds via alias", encoding="utf-8")
    alias = tmp_path / "alias"
    alias.mkdir()

    # Before the mount the two are different directories and the sibling is
    # correctly refused.
    assert is_contained_by_root_identity(real, alias / "inside.txt") is False

    with bind_mount(real, alias):
        assert _is_mounted(alias)
        assert not alias.is_symlink()  # a mount point is invisible to that rule
        real_stat = os.stat(real)
        alias_stat = os.stat(alias)
        assert (real_stat.st_dev, real_stat.st_ino) == (alias_stat.st_dev, alias_stat.st_ino)
        assert same_directory_identity(real, alias) is True
        assert is_contained_by_root_identity(real, alias / "inside.txt") is True
        assert is_contained_by_root_identity(alias, real / "inside.txt") is True
        assert (alias / "inside.txt").read_text(encoding="utf-8") == "in bounds via alias"

    # After the unmount the alias is an ordinary empty directory again.
    assert not _is_mounted(alias)
    assert is_contained_by_root_identity(real, alias / "inside.txt") is False


@pytest.mark.parametrize(("guard_name", "error", "guard"), GUARDS, ids=[row[0] for row in GUARDS])
def test_bind_alias_inside_root_allowed_by_both_chokepoints(
    tmp_path: Path,
    guard_name: str,
    error: type[ValueError],
    guard: Callable[[Path, str], Path],
) -> None:
    """A real in-root bind alias must NOT be a false positive at either guard."""
    del guard_name, error
    root = tmp_path / "ws"
    root.mkdir()
    real = root / "real"
    real.mkdir()
    alias = root / "alias"
    alias.mkdir()

    with bind_mount(real, alias):
        assert _is_mounted(alias)
        assert not alias.is_symlink()
        assert same_directory_identity(real, alias) is True
        assert guard(root, "alias/new.txt") is not None
        assert guard(root, "real/new.txt") is not None


# ── Vector 5: non-existent target under an existing root ─────────────────────


def test_nonexistent_target_under_existing_root_is_contained(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    ghost = root / "not" / "created" / "yet" / "file.txt"
    assert not ghost.exists()
    assert is_contained_by_root_identity(root, ghost) is True
    # ...but a non-existent target beyond the root is not.
    assert is_contained_by_root_identity(root, tmp_path / "gone" / "file.txt") is False


@pytest.mark.parametrize(("guard_name", "error", "guard"), GUARDS, ids=[row[0] for row in GUARDS])
def test_guard_allows_nonexistent_descendant_and_denies_escape(
    tmp_path: Path,
    guard_name: str,
    error: type[ValueError],
    guard: Callable[[Path, str], Path],
) -> None:
    """Both halves the name promises: the allow AND the escape denial.

    The escape half was missing (``del guard_name, error`` and an allow-only
    body), so a guard that stopped refusing real escapes would have kept this
    test green.
    """
    del guard_name
    root = tmp_path / "ws"
    root.mkdir()
    resolved = guard(root, "not/created/yet/file.txt")
    assert resolved.name == "file.txt"

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("classified", encoding="utf-8")
    (root / "link").symlink_to(outside, target_is_directory=True)

    for escape in ("../escape.txt", "/etc/passwd", "link/secret.txt", "a/../../etc"):
        with pytest.raises(error):
            guard(root, escape)


# ── Vector 6: unresolved input and a REAL device boundary inside the root ────


def test_identity_refuses_unresolved_path_with_a_symlink_component(tmp_path: Path) -> None:
    """Defect A: the layer must not need the caller to have resolved first.

    ``ws/link/secret.txt`` with ``ws/link`` pointing outside answered ``True``:
    the walk compared the *lexical* parent ``ws`` with the root and never looked
    at the symlink in between, so "two independent layers" was false for
    unresolved input.
    """
    root = tmp_path / "ws"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("classified", encoding="utf-8")
    (root / "link").symlink_to(outside, target_is_directory=True)

    unresolved = root / "link" / "secret.txt"
    assert not unresolved.is_symlink()          # the symlink is an ANCESTOR
    assert os.stat(unresolved).st_ino == os.stat(outside / "secret.txt").st_ino
    assert is_contained_by_root_identity(root, unresolved) is False
    assert is_contained_by_root_identity(root, root / "link") is False
    # Nonexistent descendants of a symlinked ancestor are refused too.
    assert is_contained_by_root_identity(root, root / "link" / "ghost.txt") is False
    # Control: a real (or not-yet-created) descendant is still contained.
    assert is_contained_by_root_identity(root, root / "kept.txt") is True
    assert is_contained_by_root_identity(root, root / "kept" / "deep.txt") is True


def test_foreign_device_mount_point_inside_root_is_not_contained(
    tmp_path: Path, foreign_device_dir: Path
) -> None:
    """Defect B at the layer: a mount point to another DEVICE is an escape.

    ``/dev/shm`` (its own device) mounted at ``ws/mnt`` serves a foreign
    filesystem's entries, yet ``ws/mnt`` is not a symlink and ``resolve()`` does
    not cross a mount point — so the lexical rules are blind and the old walk
    allowed ``ws/mnt/passwd`` because its lexical parent ``ws`` matched the root.
    """
    root = tmp_path / "ws"
    root.mkdir()
    (root / "in-bounds.txt").write_text("in bounds", encoding="utf-8")
    (foreign_device_dir / "passwd").write_text("classified", encoding="utf-8")

    root_stat = os.stat(root)
    foreign_stat = os.stat(foreign_device_dir)
    assert foreign_stat.st_dev != root_stat.st_dev, "fixture needs a real device boundary"

    mount_point = root / "mnt"
    mount_point.mkdir()
    with bind_mount(foreign_device_dir, mount_point):
        assert _is_mounted(mount_point)
        assert not mount_point.is_symlink()
        reached = os.stat(mount_point / "passwd")
        assert (reached.st_dev, reached.st_ino) == (foreign_stat.st_dev, os.stat(foreign_device_dir / "passwd").st_ino)

        assert is_contained_by_root_identity(root, mount_point / "passwd") is False
        assert is_contained_by_root_identity(root, mount_point / "not-created.txt") is False
        assert is_contained_by_root_identity(root, mount_point) is False
        # Control: the real in-root file is still contained.
        assert is_contained_by_root_identity(root, root / "in-bounds.txt") is True

    assert not _is_mounted(mount_point)
    # Unmounted, the same path is an ordinary in-root directory again: the
    # refusal above came from the device boundary, not from the spelling.
    assert is_contained_by_root_identity(root, mount_point / "passwd") is True


@pytest.mark.parametrize(("guard_name", "error", "guard"), GUARDS, ids=[row[0] for row in GUARDS])
def test_cross_device_mount_point_denied_by_both_chokepoints(
    tmp_path: Path,
    foreign_device_dir: Path,
    guard_name: str,
    error: type[ValueError],
    guard: Callable[[Path, str], Path],
) -> None:
    """The device-boundary vector, exercised identically at both chokepoints."""
    del guard_name
    root = tmp_path / "ws"
    root.mkdir()
    (root / "safe.txt").write_text("in bounds", encoding="utf-8")
    (foreign_device_dir / "secret.txt").write_text("classified", encoding="utf-8")
    assert os.stat(foreign_device_dir).st_dev != os.stat(tmp_path).st_dev

    mount_point = root / "mnt"
    mount_point.mkdir()
    with bind_mount(foreign_device_dir, mount_point):
        assert _is_mounted(mount_point)
        assert not mount_point.is_symlink()
        # Control: an ordinary in-root path stays allowed by both guards.
        assert guard(root, "safe.txt") is not None
        # The foreign content is refused even though every lexical rule passes.
        with pytest.raises(error):
            guard(root, "mnt/secret.txt")
        with pytest.raises(error):
            guard(root, "mnt/not-created.txt")


def test_foreign_device_descendant_of_a_real_root_is_not_contained() -> None:
    """Privilege-free real device boundary: ``/dev`` (devtmpfs) vs ``/dev/shm``.

    No mount is needed here: ``/dev/shm`` is already a real mount point on its
    own tmpfs, so the device rule stays exercised even in an environment that
    forbids ``mount --bind``.  ``/dev/shm`` is not a symlink and ``resolve()``
    does not cross it, which is exactly why the lexical rules missed it.
    """
    dev = Path("/dev")
    shm = dev / "shm"
    if not (dev.is_dir() and shm.is_dir()) or os.stat(shm).st_dev == os.stat(dev).st_dev:
        pytest.skip("no privilege-free foreign-device mount point (/dev/shm) on this host")
    assert not shm.is_symlink()
    assert is_contained_by_root_identity(dev, shm / "probe.txt") is False
    assert is_contained_by_root_identity(dev, shm) is False
    # The rule is absolute, not a quirk of a particular root spelling: even with
    # the filesystem root as the boundary, a real mount point below it is a
    # different directory tree, so it denies there too.
    assert is_contained_by_root_identity(os.sep, shm / "probe.txt") is False
    # Control: an ordinary descendant of /dev is still contained.
    assert is_contained_by_root_identity(dev, dev / "not-created.txt") is True
    # And the same boundary denies at the chokepoint that needs no write access.
    with pytest.raises(WorkspaceViolation):
        validate_relative_path(dev, "shm/probe.txt")


@pytest.mark.parametrize(("guard_name", "error", "guard"), GUARDS, ids=[row[0] for row in GUARDS])
def test_documented_residue_same_device_bind_of_outside_directory(
    tmp_path: Path,
    guard_name: str,
    error: type[ValueError],
    guard: Callable[[Path, str], Path],
) -> None:
    """KNOWN DETECTION LIMIT, pinned deliberately (not a security guarantee).

    A ``bind`` whose source is a real directory OUTSIDE the root but on the
    SAME device cannot be told apart from an ordinary in-root directory by
    ``(st_dev, st_ino)`` alone: the mount point is a real directory carrying the
    root's device number, and the lexical climb reaches the root again at its
    parent.  The layer therefore allows it and BOTH chokepoints inherit that
    allowance.  This residue is closed outside the process (mount namespace /
    no-bind mount policy) rather than here, and the test pins the limit so it
    can never be mistaken for coverage.
    """
    del guard_name, error
    root = tmp_path / "ws"
    root.mkdir()
    outside = tmp_path / "outside-same-device"
    outside.mkdir()
    (outside / "secret.txt").write_text("classified", encoding="utf-8")
    assert os.stat(outside).st_dev == os.stat(root).st_dev, "residue requires one device"

    mount_point = root / "mnt"
    mount_point.mkdir()
    with bind_mount(outside, mount_point):
        assert _is_mounted(mount_point)
        assert not mount_point.is_symlink()
        assert os.stat(mount_point).st_dev == os.stat(root).st_dev
        assert os.stat(mount_point).st_ino != os.stat(root).st_ino
        # Documented limit: identity alone cannot refuse this one...
        assert is_contained_by_root_identity(root, mount_point / "secret.txt") is True
        # ...so neither chokepoint does.  Cross-device (the case that IS closed)
        # is asserted in the vector-6 tests above.
        assert guard(root, "mnt/secret.txt") is not None


# ── Vector 7: a symlink ABOVE the ancestor that matches the root ─────────────


def test_symlink_above_the_matching_ancestor_is_allowed_by_identity(tmp_path: Path) -> None:
    """Residue pinned: a symlink whose target is *inside* the root is legal.

    ``up -> <parent of ws>`` makes ``ws/up/ws/kept.txt`` climb to ``ws/up/ws``,
    which IS the root again; the walk stops at that match, so the components it
    inspects are only those *below* it — and neither ``ws/kept.txt`` nor
    ``ws/up/ws`` is a symlink.  ``up`` itself sits above the match and is never
    inspected, so the identity layer answers ``True``.

    That answer is CORRECT, not a leak: the hop resolves back inside the root
    (the target is ``ws/kept.txt``), and refusing it would be a false positive on
    a legal spelling.  What refuses this spelling is the separate lexical
    symlink-component rule at both chokepoints.  This test goes RED if someone
    "fixes" the identity layer by forbidding the legal case above (i.e. by
    refusing every symlink anywhere in the path, including one that resolves
    back inside), which is why the split is pinned here.
    """
    root = tmp_path / "ws"
    root.mkdir()
    (root / "kept.txt").write_text("in bounds", encoding="utf-8")
    (root / "up").symlink_to(tmp_path, target_is_directory=True)

    ahead = root / "up" / "ws" / "kept.txt"
    # The hop leaves the root lexically and lands back inside it.
    assert os.path.realpath(ahead) == str(root / "kept.txt")
    assert is_contained_by_root_identity(root, ahead) is True
    # The not-yet-created variant is answered by the same matching ancestor.
    assert is_contained_by_root_identity(root, root / "up" / "ws" / "ghost.txt") is True
    # Control: the same symlink pointing OUTSIDE is refused (the vector-2 class).
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "out").symlink_to(outside, target_is_directory=True)
    assert is_contained_by_root_identity(root, root / "out" / "secret.txt") is False


@pytest.mark.parametrize(("guard_name", "error", "guard"), GUARDS, ids=[row[0] for row in GUARDS])
def test_symlink_above_the_matching_ancestor_denied_by_both_chokepoints(
    tmp_path: Path,
    guard_name: str,
    error: type[ValueError],
    guard: Callable[[Path, str], Path],
) -> None:
    """Both chokepoints cut the legal-but-ambiguous spelling lexically.

    The identity layer allows ``ws/up/ws/kept.txt`` (its target is in-root); the
    symlink-component rule refuses it before the identity layer is even reached,
    which is the layer that keeps such a path out of the process.  Nothing here
    is a false negation of the identity verdict: the identity layer answers a
    containment question, the guard answers a "may this spelling be used" one.
    """
    del guard_name
    root = tmp_path / "ws"
    root.mkdir()
    (root / "kept.txt").write_text("in bounds", encoding="utf-8")
    (root / "up").symlink_to(tmp_path, target_is_directory=True)

    with pytest.raises(error, match="symlink"):
        guard(root, "up/ws/kept.txt")
    with pytest.raises(error, match="symlink"):
        guard(root, "up/ws/ghost.txt")
    # Control: the plain in-root path is still allowed by both chokepoints.
    assert guard(root, "kept.txt") is not None


# ── Load-bearing proof: identity decides, not the string ─────────────────────


@pytest.mark.parametrize(("guard_name", "error", "guard"), GUARDS, ids=[row[0] for row in GUARDS])
def test_identity_mismatch_denies_when_the_lexical_check_still_passes(
    tmp_path: Path,
    guard_name: str,
    error: type[ValueError],
    guard: Callable[[Path, str], Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The identity layer is a real second line, not decoration.

    The candidate is lexically inside the root (``relative_to`` passes) and has
    no symlink component, yet the kernel reports the root's directory with an
    identity that is not the one captured for the root.  That is what a
    mount/bind alias or a replacement race looks like to the walk.  The guard
    must deny, and the control call in steady state must still be allowed.
    """
    del guard_name
    root = (tmp_path / "ws").resolve()
    root.mkdir()

    # Control: with the real kernel answer the same call is allowed.
    assert guard(root, "a/b.txt").name == "b.txt"

    real_directory_identity = path_boundary._directory_identity
    root_path = os.path.abspath(str(root))

    def foreign_identity(path: os.PathLike[str] | str) -> tuple[int, int] | None:
        if os.path.abspath(os.fspath(path)) == root_path:
            return (0, 0)  # not the root's captured (st_dev, st_ino)
        return real_directory_identity(path)

    with monkeypatch.context() as patch:
        patch.setattr(path_boundary, "_directory_identity", foreign_identity)
        with pytest.raises(error):
            guard(root, "a/b.txt")


# ── No weakening: every previously-closed vector and every safe path holds ──


STILL_DENIED = [
    pytest.param("../escape.txt", id="dotdot"),
    pytest.param("a/../../etc", id="dotdot-nested"),
    pytest.param("/etc/passwd", id="posix-absolute"),
    pytest.param("C:/Windows/System32", id="windows-drive"),
    pytest.param("a/./b", id="normalize-dot-segment"),
    pytest.param("a//b", id="normalize-empty-segment"),
    pytest.param("%2e%2e%2fsecret", id="percent-traversal"),
]

STILL_ALLOWED = [
    pytest.param("a/b/c.txt", id="plain"),
    pytest.param("file with spaces.txt", id="spaces"),
    pytest.param("\u0444\u0430\u0439\u043b.txt", id="cyrillic"),
    pytest.param("100%.txt", id="literal-percent"),
]


@pytest.mark.parametrize("path", STILL_DENIED)
@pytest.mark.parametrize(("guard_name", "error", "guard"), GUARDS, ids=[row[0] for row in GUARDS])
def test_lexical_vectors_still_denied_after_identity_wiring(
    tmp_path: Path,
    path: str,
    guard_name: str,
    error: type[ValueError],
    guard: Callable[[Path, str], Path],
) -> None:
    del guard_name
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with pytest.raises(error):
        guard(workspace, path)


@pytest.mark.parametrize("path", STILL_ALLOWED)
@pytest.mark.parametrize(("guard_name", "error", "guard"), GUARDS, ids=[row[0] for row in GUARDS])
def test_safe_paths_still_allowed_after_identity_wiring(
    tmp_path: Path,
    path: str,
    guard_name: str,
    error: type[ValueError],
    guard: Callable[[Path, str], Path],
) -> None:
    del guard_name, error
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    assert guard(workspace, path) is not None


def test_identity_walk_terminates_at_filesystem_root() -> None:
    # A path whose deepest existing ancestor is / terminates and is not
    # contained in an unrelated root.
    assert is_contained_by_root_identity(os.sep, os.sep) is True
    assert is_contained_by_root_identity(os.sep, str(Path(os.sep) / "nope" / "x")) is True
    assert stat.S_ISDIR(os.stat(os.sep).st_mode)
