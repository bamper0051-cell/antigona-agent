"""Shared boundary policy for workspace-relative paths.

Two layers, both fail-closed:

* **Lexical** (:func:`has_unsafe_relative_path_syntax`) — rejects syntax that can
  change path *structure*: literal/encoded ``..``, absolute, Windows drive and
  UNC forms, control characters, compatibility folds of any of those.
* **OS identity** (:func:`is_contained_by_root_identity`) — decides containment
  by the operating system's own answer (same ``st_dev`` + ``st_ino``) after
  walking a path up to its deepest existing ancestor, instead of comparing
  strings.  This judges aliases that no string comparison can recover from the
  spelling: symlinked ancestors, case-folded names, Unicode NFC/NFD equivalence,
  and mount/bind aliases of one directory.

The identity layer never compares path strings and never answers ``True`` for a
root that has no OS identity (missing, not a directory, unreadable): that raises
:class:`RootIdentityError` so every caller denies (fail closed).

It also does not depend on the caller having resolved symlinks first: the walk
refuses any component *below* the root that is itself a symlink, and any
component that lives on a different device than the root (a mount point inside
the root reaching a foreign filesystem), instead of accepting it because its
lexical parent is the root.  Both refusals are fail-closed.

A symlink *above* the ancestor that matches the root is deliberately allowed by
this layer.  With ``ws/up -> <parent of ws>``, the path ``ws/up/ws/kept.txt``
climbs to ``ws/up/ws``, which IS the root again: the walk stops at that match,
so only the components *below* it (``kept.txt``, ``ws``) are inspected and ``up``
— sitting above the match — is never looked at.  The walk therefore answers
``True``, and that is correct rather than a leak: the hop resolves back *inside*
the root (the target is ``ws/kept.txt``), so refusing it would be a false
positive on a legitimate spelling.  That spelling is still cut off by the two
chokepoints' own lexical rule: :func:`antigona.filesystem.validate_relative_path`
and :meth:`antigona.worker.tools.common.WorkspaceGuard.resolve` both refuse a
symlink path component (``symlink path component forbidden``), so nothing
downstream accepts it.  The split — identity allows, lexical guards deny — is
pinned by ``tests/security/test_workspace_fence_identity.py``.

Residue, stated honestly: a ``bind`` mount whose *source* is a real directory
outside the root on the **same** device is indistinguishable from an ordinary
in-root directory by ``st_dev`` + ``st_ino`` alone (the mount point is a real
directory with the root's device number and its own inode, and the lexical
climb reaches the root again at the mount point's parent).  That class is NOT
closed by this layer; contain it outside the process with a mount namespace, a
read-only/no-bind mount policy, or by asserting the root is not a mount tree.

Mechanism ported from the MIT-licensed Go package ``internal/pathidentity``
(``os.SameFile`` + climb to the deepest existing ancestor), adapted to raise a
typed error where the Go original returns ``False``.
"""

from __future__ import annotations

import os
import re
import stat
import unicodedata
from pathlib import PureWindowsPath
from typing import TypeAlias

_PERCENT_ESCAPE = re.compile(r"%[0-9a-fA-F]{2}")

#: ``(st_dev, st_ino)`` — the kernel's identity for a filesystem object.
FileIdentity: TypeAlias = tuple[int, int]


class PathIdentityError(ValueError):
    """Base error for an OS-identity path decision that cannot be made."""


class RootIdentityError(PathIdentityError):
    """A containment question was asked of a root with no OS identity.

    Raised when the root is missing, is not a directory, or cannot be looked
    up.  Callers MUST treat it as DENY; it is never a ``True`` answer.
    """


def _directory_identity(path: os.PathLike[str] | str) -> FileIdentity | None:
    """Return *path*'s ``(st_dev, st_ino)`` when it is an existing directory.

    ``os.stat`` follows symlinks (like Go's ``os.Stat``), so the answer is the
    identity of what the path *reaches*.  ``None`` when the path is absent, is
    not a directory, or cannot be looked up.
    """
    try:
        info = os.stat(path)
    except OSError:
        return None
    if not stat.S_ISDIR(info.st_mode):
        return None
    return (info.st_dev, info.st_ino)


def root_identity(root: os.PathLike[str] | str) -> FileIdentity:
    """Return the ``(st_dev, st_ino)`` identity of an existing root directory.

    Raises:
        RootIdentityError: *root* does not exist, is not a directory, or cannot
            be looked up.  Fail closed — this is a DENY, never ``True``.
    """
    try:
        info = os.stat(root)
    except OSError as exc:
        raise RootIdentityError(
            f"workspace root has no OS identity (missing or unreadable): {root}"
        ) from exc
    if not stat.S_ISDIR(info.st_mode):
        raise RootIdentityError(f"workspace root is not a directory: {root}")
    return (info.st_dev, info.st_ino)


def same_directory_identity(left: os.PathLike[str] | str, right: os.PathLike[str] | str) -> bool:
    """Return whether *left* and *right* are the same existing directory.

    The answer is the operating system's (same device and inode), never a string
    comparison: neither operand is canonicalized, because the lookup already is
    the canonicalization.  ``False`` when either operand does not exist or is
    not a directory — including for a path that does not exist compared against
    itself, which has no identity.
    """
    left_identity = _directory_identity(left)
    if left_identity is None:
        return False
    return left_identity == _directory_identity(right)


def deepest_existing_ancestor(path: str) -> str | None:
    """Return the deepest existing ancestor of *path*, or ``None``.

    *path* itself is returned when it exists.  The walk is bounded by the number
    of separators in *path* and terminates at the filesystem root.
    """
    current = os.path.abspath(path)
    while True:
        if _directory_identity(current) is not None:
            return current
        parent = os.path.dirname(current)
        if parent in ("", current):
            return None
        current = parent


def _breaks_root_identity(node: str, root_identity_value: FileIdentity) -> bool:
    """Return whether a climb node *below* the root breaks containment.

    Two OS facts that no string comparison can see are refused here, so the
    walk never calls a path contained merely because its lexical parent is the
    root:

    * the node is a **symlink** — the kernel would follow it somewhere this walk
      does not look.  This is the unresolved-input defect: ``ws/link/secret.txt``
      with ``ws/link`` pointing outside answered ``True`` because only the
      *lexical* parent ``ws`` was compared against the root, so the layer was
      correct only for input the caller had already resolved.
    * the node lives on a **different device** than the root — a mount point
      inside the root serves another filesystem's directory entries, so
      ``ws/mnt/passwd`` reached a foreign inode through a real device boundary
      while ``ws`` still matched the root, and neither ``resolve`` nor
      ``is_symlink`` sees a mount point.

    A node that does not exist yet has neither property — it is answered by its
    deepest existing ancestor.  Any other lookup failure is unknown state and
    denies (fail closed).  The device rule is absolute: it applies to any mount
    point below the root, including when the root is the filesystem root.
    """
    try:
        info = os.lstat(node)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    if stat.S_ISLNK(info.st_mode):
        return True
    return info.st_dev != root_identity_value[0]


def is_contained_by_root_identity(root: os.PathLike[str] | str, path: os.PathLike[str] | str) -> bool:
    """Return whether *path* is *root* itself or lies beneath it, by identity.

    *path* need not exist: the walk climbs its lexical ancestors and stops at
    the first one that is the same directory as *root* (same device and inode),
    so a descendant that has not been created yet is answered by the deepest
    ancestor that does exist.  The walk is bounded by the number of separators
    in *path* and terminates at the filesystem root.

    Every component climbed *below* that matching ancestor must also be a real
    (non-symlink) directory on *root*'s own device; a symlink component or a
    component on a foreign device denies (see :func:`_breaks_root_identity`).
    The caller therefore does not have to resolve symlinks first — an
    unresolved path with a symlink component is refused on its own, and a
    mount/bind point inside the root is refused at the device boundary.  A
    ``bind`` of an outside directory on the *same* device remains undetectable
    by identity alone; see the module docstring for that documented residue.

    Raises:
        RootIdentityError: *root* has no OS identity.  Fail closed — never
            ``True``.
    """
    root_identity_value = root_identity(root)
    current = os.path.abspath(path)
    below_root: list[str] = []
    while True:
        if _directory_identity(current) == root_identity_value:
            return not any(
                _breaks_root_identity(node, root_identity_value) for node in below_root
            )
        below_root.append(current)
        parent = os.path.dirname(current)
        if parent in ("", current):
            return False
        current = parent


def has_unsafe_relative_path_syntax(path: str) -> bool:
    """Return whether *path* has ambiguous or cross-platform escape syntax.

    Literal percent signs and harmless Unicode compatibility characters remain
    valid. Valid percent escapes are denied because a downstream decode can
    change path structure. Backslashes, Windows drives/UNC forms, controls,
    empty/dot segments, and compatibility forms that fold into those structures
    are denied consistently on every host OS.
    """
    if not path or any(ord(character) < 32 or ord(character) == 127 for character in path):
        return True
    if "\\" in path:
        return True

    normalized = unicodedata.normalize("NFKC", path)
    if "\\" in normalized or _PERCENT_ESCAPE.search(normalized):
        return True
    if PureWindowsPath(normalized).drive or PureWindowsPath(normalized).is_absolute():
        return True

    parts = path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        return True

    normalized_parts = normalized.split("/")
    return any(part in {"", ".", ".."} for part in normalized_parts)
