"""Content-addressed file store for card bodies.

Implemented in P2.1.f under the P1.3 artifact rules: ``skills/`` inside the state
directory at 0750 with 0640 files, refusal of symlinks, hardlinked targets
(``st_nlink > 1``) and paths escaping the root, atomic temp + ``os.replace`` writes, and
a mandatory ``body_sha256`` check on every read whose failure raises
``SkillIntegrityError`` and quarantines the skill.
"""

from __future__ import annotations

import hashlib
import os
import stat
import tempfile
from pathlib import Path

from .canonical import is_sha256_hex, split_footer
from .errors import SkillIntegrityError, SkillSyntaxError

__all__ = [
    "CardStore",
    "card_body_path",
    "open_body_safely",
    "read_body_safely",
    "write_body_atomically",
]

#: Subdirectory inside the store root.
STORE_DIR = "skills"
#: Directory mode.
_DIR_MODE = 0o750
#: File mode.
_FILE_MODE = 0o640


def _store_root(base: Path) -> Path:
    """Return the ``skills/`` subdirectory, creating it if absent."""
    root = base.resolve() / STORE_DIR
    root.mkdir(mode=_DIR_MODE, parents=True, exist_ok=True)
    return root


def _shard_dir(root: Path, digest: str) -> Path:
    """Return the two-char shard subdirectory for a 64-char hex digest.

    This keeps the number of entries per directory manageable at scale.
    """
    assert is_sha256_hex(digest)
    return root / digest[:2]


def card_body_path(base: Path, body_sha256: str) -> Path:
    """Full filesystem path for a stored card body given its sha256."""
    shard = _shard_dir(_store_root(base), body_sha256)
    return shard / body_sha256[2:]


def _open_body(root: Path, body_sha256: str) -> tuple[int, list[int]]:
    """Open a stored card body for reading, rejecting symlinks and hardlinks.

    Returns ``(fd, list_of_opened_directory_fds)`` so the caller can clean up.
    Raises ``OSError`` on any violation.
    """
    target = card_body_path(root, body_sha256)
    parts = target.relative_to(root.resolve()).parts

    if not parts or any(part in ("", ".", "..") for part in parts):
        raise OSError("invalid card body path")

    if os.name == "nt":
        # BUG ANT-007 (wave3, class G1): os.O_CLOEXEC / O_DIRECTORY / dir_fd
        # do not exist on Windows and the openat chain below raised
        # AttributeError on every card read (skills store, capture, registry).
        # Fall back to a resolved-path read with a symlink component check.
        import pathlib as _pl
        base = root.resolve()
        candidate = _pl.Path(base) / _pl.Path(*parts)
        resolved = candidate.resolve(strict=True)
        if resolved != base and base not in resolved.parents:
            raise OSError("card body path outside root")
        # Component-wise symlink check on the actual path.
        cur = base
        for part in parts[:-1]:
            cur = cur / part
            if cur.is_symlink():
                raise OSError("symlink")
        body_fd = os.open(
            resolved, os.O_RDONLY | (getattr(os, "O_BINARY", 0))
        )
        try:
            body_stat = os.fstat(body_fd)
            if not stat.S_ISREG(body_stat.st_mode):
                os.close(body_fd)
                raise OSError("card body is not a regular file")
            if body_stat.st_nlink != 1:
                os.close(body_fd)
                raise OSError("card body hardlinks are not allowed")
        except OSError:
            os.close(body_fd)
            raise
        return body_fd, []

    opened: list[int] = []
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        directory = os.open(root.resolve(), flags | os.O_DIRECTORY)
        opened.append(directory)
        for component in parts[:-1]:
            directory = os.open(component, flags | os.O_DIRECTORY, dir_fd=directory)
            opened.append(directory)
        body_fd = os.open(parts[-1], flags, dir_fd=directory)
        body_stat = os.fstat(body_fd)
        if not stat.S_ISREG(body_stat.st_mode):
            os.close(body_fd)
            raise OSError("card body is not a regular file")
        if body_stat.st_nlink != 1:
            os.close(body_fd)
            raise OSError("card body hardlinks are not allowed")
        return body_fd, opened
    except OSError as e:
        if e.errno == 40:  # ELOOP - too many levels of symbolic links
            raise OSError("symlink") from e
        for fd in reversed(opened):
            os.close(fd)
        raise


def open_body_safely(root: Path, body_sha256: str) -> bytes:
    """Read a stored card body with integrity verification, returning raw bytes.

    Raises :class:`SkillIntegrityError` when the file's sha256 does not match the
    expected digest or when link/inode checks fail — the skill must be quarantined.
    """
    if not is_sha256_hex(body_sha256):
        raise SkillIntegrityError(
            f"body_sha256 must be a 64-char hex string, got {body_sha256!r}"
        )

    try:
        fd, directories = _open_body(root, body_sha256)
    except OSError as e:
        raise SkillIntegrityError(str(e)) from e
    try:
        before = os.fstat(fd)
        if before.st_nlink != 1:
            raise SkillIntegrityError("card body hardlinks are not allowed")
        if before.st_size < 0:
            raise SkillIntegrityError("card body has invalid size")

        chunks: list[bytes] = []
        remaining = before.st_size + 1 if before.st_size > 0 else 1
        while remaining:
            chunk = os.read(fd, min(remaining, 1024 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)

        after = os.fstat(fd)
        if after.st_nlink != 1:
            if after.st_nlink == 0:
                raise SkillIntegrityError("card body path changed while being read")
            raise SkillIntegrityError("card body hardlinks are not allowed")
        if (before.st_dev, before.st_ino, before.st_size, before.st_nlink) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_nlink,
        ):
            raise SkillIntegrityError("card body changed while being read")
    finally:
        os.close(fd)
        for directory in reversed(directories):
            os.close(directory)

    # Re-walk the namespace after reading — detect TOCTOU replacement.
    check_fd, check_directories = _open_body(root, body_sha256)
    try:
        current = os.fstat(check_fd)
        if (after.st_dev, after.st_ino, after.st_size) != (
            current.st_dev,
            current.st_ino,
            current.st_size,
        ):
            raise SkillIntegrityError("card body path changed while being read")
    finally:
        os.close(check_fd)
        for directory in reversed(check_directories):
            os.close(directory)

    actual_digest = hashlib.sha256(data).hexdigest()
    # Verify that the stored data is the full card (head + footer)
    # The digest is computed from head only (per SKILL_FORMAT.md §6)
    try:
        head, _, _ = split_footer(data)
        head_digest = hashlib.sha256(head).hexdigest()
        if head_digest != body_sha256:
            raise SkillIntegrityError(
                f"card body sha256 mismatch: expected {body_sha256}, got {head_digest}"
            )
    except Exception:
        # If split_footer fails, the data is invalid anyway
        raise SkillIntegrityError(
            f"card body sha256 mismatch: expected {body_sha256}, got {actual_digest}"
        ) from None

    return data


def read_body_safely(root: Path, body_sha256: str) -> bytes:
    """Read a stored card body with full integrity verification.

    Alias for :func:`open_body_safely` — see its docstring.
    """
    return open_body_safely(root, body_sha256)


def _existing_body_matches(root: Path, digest: str) -> bool:
    """Return ``True`` when the already-stored body really hashes to ``digest``.

    The write path never trusts the file size: the stored bytes are read back and the
    hashed range (head, excluding the footer — see :func:`canonical.body_digest`) is
    re-hashed. Anything unreadable, unsplittable or divergent counts as corruption and
    the caller rewrites the file atomically.
    """
    try:
        verify_fd, verify_directories = _open_body(root, digest)
    except OSError:
        return False
    try:
        chunks: list[bytes] = []
        while True:
            chunk = os.read(verify_fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        os.close(verify_fd)
        for directory in reversed(verify_directories):
            os.close(directory)

    try:
        head, _, _ = split_footer(b"".join(chunks))
    except SkillSyntaxError:
        return False
    return hashlib.sha256(head).hexdigest() == digest


def write_body_atomically(root: Path, data: bytes) -> str:
    """Atomically write ``data`` into the store, returning its ``sha256``.

    The file is created via ``tempfile.NamedTemporaryFile`` + ``os.replace`` so that
    a crash during write never leaves a partial file. File mode is 0640.

    For ASKILL/1 cards, the digest is computed from the hashed range (head, excluding
    the footer), per docs/SKILL_FORMAT.md §6.
    """
    if not isinstance(data, bytes):
        raise TypeError("card body must be bytes")

    # Compute digest from the hashed range (head, before footer)
    head, _, _ = split_footer(data)
    digest = hashlib.sha256(head).hexdigest()

    store = _store_root(root)
    shard = _shard_dir(store, digest)
    shard.mkdir(mode=_DIR_MODE, parents=True, exist_ok=True)

    final_path = shard / digest[2:]
    if final_path.exists() and _existing_body_matches(root, digest):
        # Already stored and the stored bytes re-hash to the same digest.
        return digest

    tmp_path: str | None = None
    try:
        tmp = tempfile.NamedTemporaryFile(
            dir=str(store),
            prefix=".tmp_",
            suffix=".askill",
            delete=False,
        )
        tmp_path = tmp.name
        tmp.write(data)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp.close()
        os.chmod(tmp_path, _FILE_MODE)
        os.replace(tmp_path, str(final_path))
    except BaseException:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        raise

    return digest


class CardStore:
    """File-level, content-addressed store for ``ASKILL/1`` card bodies.

    One store instance per state root. The store path is ``root/skills/<sha256>``
    with a two-char shard directory. Every read double-checks the sha256 and refuses
    symlinks, hardlinks, and TOCTOU replacement.
    """

    def __init__(self, state_root: str | Path) -> None:
        self._root = Path(state_root).resolve()

    @property
    def root(self) -> Path:
        return self._root

    def write(self, data: bytes) -> str:
        """Store card body bytes, returning the sha256 digest.

        Atomic (temp + os.replace). Existing identical content is silently
        returned (no-op).
        """
        return write_body_atomically(self._root, data)

    def read(self, body_sha256: str) -> bytes:
        """Read and verify a stored card body.

        Raises :class:`SkillIntegrityError` on sha256 mismatch or filesystem
        integrity violation.
        """
        return read_body_safely(self._root, body_sha256)

    def path(self, body_sha256: str) -> Path:
        """Return the expected filesystem path for a digest (may not exist)."""
        return card_body_path(self._root, body_sha256)