"""A bounded, non-blocking advisory store lock with typed refusals (wave G1b, M2).

The lock answers exactly one question — *did this caller get in?* — and the answer
is derived from the syscall, never from the lock file's text. A persisted PID is
metadata, not authority: a stale ``pid`` in the file neither grants nor blocks the
lock (invariant L2, and the reference's own warning that persisted owner metadata is
not current-holder proof).

Classification (PLAN.md §5.3):

======================  =============================  =========================
condition               exception                      mutation status proved
======================  =============================  =========================
``flock`` refused       :class:`LockContendedError`    proven not started
``mkdir``/``open`` fail :class:`LockPreAcquisitionError` not started (env defect)
other ``flock`` error   :class:`LockPreAcquisitionError` not started
======================  =============================  =========================
"""

from __future__ import annotations

import errno
import json
import os
import socket
import uuid
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

try:  # pragma: no cover - platform dependent
    import fcntl
except ImportError:  # Windows: no flock; locking degrades to the msvcrt analogue
    fcntl = None  # type: ignore[assignment]

from .errors import ChainError, LockContendedError, LockPreAcquisitionError
from .fsops import mkdir_all_sync, open_no_follow, write_all

__all__ = [
    "LOCK_OWNER_SCHEMA",
    "LockOwner",
    "StoreLock",
    "acquire_store_lock",
    "is_lock_contention",
]

#: Envelope for the owner metadata block written into the lock file.
LOCK_OWNER_SCHEMA: Final[str] = "antigona.chain-lock-owner/v1"


def is_lock_contention(exc: OSError) -> bool:
    """True only for a genuine concurrent holder (the non-blocking lock was refused).

    Semantically identical to ``transport.telegram._is_lock_contention`` — the same
    predicate the Go reference uses in ``tryLockFile``. Only ``EAGAIN`` /
    ``EWOULDBLOCK`` / ``BlockingIOError`` mean "someone else holds it"; every other
    ``OSError`` is an environment failure and must not be reported as contention.
    """
    if isinstance(exc, BlockingIOError):
        return True
    return exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK)


@dataclass(frozen=True, slots=True)
class LockOwner:
    """Who held the lock when it was taken. Metadata for operators, never authority."""

    owner_id: str
    pid: int
    host: str
    acquired_at: str
    schema: str = LOCK_OWNER_SCHEMA

    def to_payload(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "owner_id": self.owner_id,
            "pid": self.pid,
            "host": self.host,
            "acquired_at": self.acquired_at,
        }


@dataclass(slots=True)
class StoreLock:
    """A held lock. ``release`` is unconditional and idempotent (invariant L5)."""

    path: Path
    fd: int
    owner: LockOwner
    _released: bool = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        if fcntl is not None:
            with suppress(OSError):
                fcntl.flock(self.fd, fcntl.LOCK_UN)
        with suppress(OSError):
            os.close(self.fd)

    def __enter__(self) -> StoreLock:
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


def _new_owner() -> LockOwner:
    return LockOwner(
        owner_id=uuid.uuid4().hex,
        pid=os.getpid(),
        host=socket.gethostname(),
        acquired_at=datetime.now(UTC).isoformat(),
    )


def acquire_store_lock(path: Path | str, *, mode: int = 0o600) -> StoreLock:
    """Take the exclusive non-blocking advisory lock at ``path``, or refuse typed.

    Raises :class:`LockPreAcquisitionError` for every failure that happens before a
    usable lock exists (directory creation, the no-follow open walk, a non-contention
    ``flock`` error, or persisting the owner metadata — in that last case the lock is
    unlocked again rather than handed out, so the guarded body still never runs).
    Raises :class:`LockContendedError` when the advisory lock is held by a live
    holder. Nothing else is ever reported as "busy".
    """
    lock_path = Path(path)
    try:
        mkdir_all_sync(lock_path.parent)
    except (OSError, ChainError) as exc:
        raise LockPreAcquisitionError(lock_path, exc) from exc

    try:
        fd = open_no_follow(
            lock_path,
            os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
            mode,
        )
    except (OSError, ChainError) as exc:
        raise LockPreAcquisitionError(lock_path, exc) from exc

    if fcntl is not None:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            if is_lock_contention(exc):
                raise LockContendedError(lock_path) from exc
            raise LockPreAcquisitionError(lock_path, exc) from exc
    elif os.name == "nt":  # pragma: no cover - Windows only
        # msvcrt byte-range locking is the flock analogue; LK_NBLCK is non-blocking.
        import msvcrt

        try:
            os.lseek(fd, 0, os.SEEK_END)
            if os.lseek(fd, 0, os.SEEK_CUR) == 0:
                write_all(fd, b"\0")
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            os.close(fd)
            if is_lock_contention(exc):
                raise LockContendedError(lock_path) from exc
            raise LockPreAcquisitionError(lock_path, exc) from exc

    owner = _new_owner()
    try:
        payload = (json.dumps(owner.to_payload(), sort_keys=True) + "\n").encode("utf-8")
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        write_all(fd, payload)
        os.fsync(fd)
    except OSError as exc:
        if fcntl is not None:
            with suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
        with suppress(OSError):
            os.close(fd)
        raise LockPreAcquisitionError(lock_path, exc) from exc

    return StoreLock(path=lock_path, fd=fd, owner=owner)
