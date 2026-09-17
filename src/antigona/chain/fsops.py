"""Durability and no-replace primitives for the chain (wave G1b, M1a/M1b).

These are the filesystem mechanics only; they hold no chain semantics. The
no-follow/no-hardlink discipline is the same property already proven in
``antigona.skills.store`` (``skills/store.py:59-126``) — lifted here as a neutral
helper rather than duplicated, while ``skills/store.py`` keeps its own
card-format-specific digest contract and is deliberately untouched by this wave.

Invariants (PLAN.md §4.3):

* **F1** — no durability claim without a sync: every path that reports success has
  fsynced the file (if any) and the containing directory.
* **F2** — no overwrite of an immutable event: publication is ``link``/``O_EXCL``
  based, never :func:`os.replace`.
* **F3** — no symlink or hardlink traversal: opens are ``O_NOFOLLOW`` and
  ``st_nlink != 1`` is refused.
* **F4** — failure is classified: a failed sync raises :class:`ChainSyncError`
  naming the path and is never swallowed into success.

Note on :func:`write_immutable_no_replace`: the *no-replace* analogue of
``renameat2(RENAME_NOREPLACE)`` is :func:`os.link`, which cannot clobber a
destination; where hardlinks are unavailable (``EXDEV``/``EPERM``/``ENOSYS``/
``EOPNOTSUPP``) the fallback is ``open(O_CREAT|O_EXCL)`` + copy + sync.
"""

from __future__ import annotations

import errno
import os
import stat
import sys
import tempfile
from contextlib import suppress
from pathlib import Path

from .errors import ChainIntegrityError, ChainPathError, ChainSyncError

__all__ = [
    "ATOMIC_TEMP_PREFIX",
    "EVENT_TEMP_PREFIX",
    "fsync_dir",
    "mkdir_all_sync",
    "open_no_follow",
    "read_bytes_verified",
    "read_file_no_follow",
    "write_all",
    "write_atomic",
    "write_immutable_no_replace",
]

#: Temp-file prefixes. The leading dot keeps a partial file out of any glob that
#: lists published events (``<64-hex>.json``).
ATOMIC_TEMP_PREFIX = ".atomic-"
EVENT_TEMP_PREFIX = ".event-"

#: Errors that mean "hardlinks are not available here, fall back to O_EXCL copy".
_LINK_UNSUPPORTED: tuple[int, ...] = (
    errno.EXDEV,
    errno.EPERM,
    errno.ENOSYS,
    getattr(errno, "EOPNOTSUPP", errno.ENOTSUP),
    errno.ENOTSUP,
)

#: Errors a directory sync may legitimately report on platforms that cannot sync
#: a directory handle (see the Go reference's ``SyncReviewDirectory``). On Linux a
#: failure is a hard error.
_DIR_SYNC_UNSUPPORTED: tuple[int, ...] = (
    errno.EINVAL,
    getattr(errno, "EOPNOTSUPP", errno.ENOTSUP),
    errno.ENOTSUP,
)

_DIR_FLAGS: int = (
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)


def write_all(fd: int, payload: bytes) -> None:
    """Write every byte of ``payload`` to ``fd`` (no short-write truncation)."""
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def fsync_dir(path: Path | str) -> None:
    """Persist a directory entry by fsyncing the directory handle itself.

    A created file is not durable until its parent's own directory data is synced;
    a rename is atomic but its *directory entry* is not durable without this.
    """
    directory = Path(path)
    try:
        fd = os.open(os.fspath(directory), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError as exc:
        raise ChainSyncError(directory, "directory open", exc) from exc
    try:
        try:
            os.fsync(fd)
        except OSError as exc:
            if os.name != "posix" or not sys.platform.startswith("linux"):
                # Only non-Linux may legitimately reject a directory handle.
                if exc.errno in _DIR_SYNC_UNSUPPORTED or exc.errno == errno.EPERM:
                    return
            raise ChainSyncError(directory, "directory sync", exc) from exc
    finally:
        os.close(fd)


def mkdir_all_sync(path: Path | str, mode: int = 0o755) -> None:
    """Create ``path`` component by component, syncing the parent of each created one.

    Components are created one at a time with :func:`os.mkdir` because its own
    success/``EEXIST`` outcome is race free, whereas a preceding ``stat`` would
    leave a TOCTOU window against a concurrent creator.
    """
    target = Path(path)
    parent = target.parent
    if parent == target:  # filesystem root (or ".") — nothing to create
        return
    mkdir_all_sync(parent, mode)
    try:
        os.mkdir(target, mode)
    except FileExistsError:
        if not os.path.isdir(target):
            raise ChainPathError(
                f"{str(target)!r} exists but is not a directory; refusing to use it as one"
            ) from None
        return
    fsync_dir(parent)


def open_no_follow(path: Path | str, flags: int, mode: int = 0o600) -> int:
    """Open ``path`` refusing symlinks *and* hardlinks; the caller closes the fd.

    Every path component is opened with ``O_NOFOLLOW`` via ``dir_fd`` so a
    pre-created symlink cannot be traversed, and the final object must be a regular
    file with ``st_nlink == 1`` (a hardlinked "immutable" event is not immutable).
    """
    absolute = os.path.abspath(os.fspath(path))
    components = [part for part in absolute.split(os.sep) if part]
    if not components:
        raise ChainPathError(f"refusing to open the filesystem root: {absolute!r}")
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)

    if os.name == "nt" or os.open not in os.supports_dir_fd:
        fd = _open_no_follow_portable(absolute, flags, mode)
    else:
        fd = _open_no_follow_at(components, absolute, flags, mode)

    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ChainPathError(f"refusing {absolute!r}: not a regular file")
        if info.st_nlink != 1:
            raise ChainPathError(
                f"refusing {absolute!r}: hardlinked file (st_nlink={info.st_nlink})"
            )
    except BaseException:
        os.close(fd)
        raise
    return fd


def _open_no_follow_at(components: list[str], absolute: str, flags: int, mode: int) -> int:
    directory = -1
    fd = -1
    try:
        directory = os.open(os.sep, _DIR_FLAGS)
        for component in components[:-1]:
            following = os.open(component, _DIR_FLAGS, dir_fd=directory)
            os.close(directory)
            directory = following
        fd = os.open(components[-1], flags, mode, dir_fd=directory)
    except OSError as exc:
        if fd >= 0:
            os.close(fd)
        raise ChainPathError(f"refusing {absolute!r}: {exc}") from exc
    finally:
        if directory >= 0:
            os.close(directory)
    return fd


def _open_no_follow_portable(absolute: str, flags: int, mode: int) -> int:
    """Windows fallback: no ``dir_fd``/``O_DIRECTORY``, so check components by path."""
    candidate = Path(absolute)
    for parent in reversed(candidate.parents):
        if parent.is_symlink():
            raise ChainPathError(f"refusing {absolute!r}: {str(parent)!r} is a symlink")
    if candidate.is_symlink():
        raise ChainPathError(f"refusing {absolute!r}: it is a symlink")
    try:
        return os.open(absolute, flags | getattr(os, "O_BINARY", 0), mode)
    except OSError as exc:
        raise ChainPathError(f"refusing {absolute!r}: {exc}") from exc


def read_file_no_follow(path: Path | str) -> bytes | None:
    """Read a regular, unlinked file without following links; ``None`` if absent."""
    target = Path(path)
    try:
        fd = open_no_follow(target, os.O_RDONLY)
    except ChainPathError as exc:
        cause = exc.__cause__
        if isinstance(cause, FileNotFoundError) or getattr(cause, "errno", None) == errno.ENOENT:
            return None
        raise
    try:
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def read_bytes_verified(path: Path | str, expected: bytes) -> None:
    """Require that the stored bytes are byte-identical to ``expected``.

    A digest names its own bytes; if the file under that name does not reproduce
    them, the store is corrupt and must fail closed rather than trust the file.
    """
    actual = read_file_no_follow(path)
    if actual is None:
        raise ChainIntegrityError(f"expected {str(Path(path))!r} to exist, but it is absent")
    if actual != expected:
        raise ChainIntegrityError(
            f"existing content-addressed event {str(Path(path))!r} does not match "
            f"its revision ({len(actual)} bytes stored, {len(expected)} expected)"
        )


def write_atomic(path: Path | str, payload: bytes, mode: int, *, dir_mode: int = 0o755) -> None:
    """Create the parent, write a temp file, sync it, then replace atomically.

    The rename is atomic, so no reader ever observes a partial file; the directory
    sync afterwards is what makes the *entry* durable.
    """
    target = Path(path)
    parent = target.parent
    mkdir_all_sync(parent, dir_mode)
    fd, tmp_name = tempfile.mkstemp(dir=os.fspath(parent), prefix=ATOMIC_TEMP_PREFIX)
    try:
        try:
            os.fchmod(fd, mode)
            write_all(fd, payload)
            try:
                os.fsync(fd)
            except OSError as exc:
                raise ChainSyncError(target, "file sync", exc) from exc
        finally:
            os.close(fd)
        os.replace(tmp_name, target)
    except BaseException:
        with suppress(OSError):
            os.unlink(tmp_name)
        raise
    fsync_dir(parent)


def write_immutable_no_replace(tmp_path: Path | str, final_path: Path | str) -> None:
    """Publish ``tmp_path`` at ``final_path`` *without* ever overwriting it.

    Raises :class:`FileExistsError` when the destination already exists — and leaves
    it byte-identical. ``os.replace`` is deliberately not used anywhere on this
    path: overwrite is exactly what an immutable, content-addressed event forbids.
    """
    source = Path(tmp_path)
    destination = Path(final_path)
    try:
        os.link(source, destination)
    except FileExistsError:
        raise
    except OSError as exc:
        if exc.errno not in _LINK_UNSUPPORTED:
            raise
        _publish_by_copy(source, destination)
        return
    os.unlink(source)


def _publish_by_copy(source: Path, destination: Path) -> None:
    """``O_EXCL`` publication for filesystems that cannot hardlink across the boundary."""
    payload = read_file_no_follow(source)
    if payload is None:
        raise ChainIntegrityError(f"temporary publication source {str(source)!r} is absent")
    mode = stat.S_IMODE(os.stat(source).st_mode)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(os.fspath(destination), flags, mode)
    try:
        try:
            os.fchmod(fd, mode)
            write_all(fd, payload)
            try:
                os.fsync(fd)
            except OSError as exc:
                raise ChainSyncError(destination, "file sync", exc) from exc
        except BaseException:
            os.close(fd)
            with suppress(OSError):
                os.unlink(destination)
            raise
    finally:
        with suppress(OSError):
            os.close(fd)
    os.unlink(source)
