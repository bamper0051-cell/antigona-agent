"""FileMemory — file-based two-file memory system.

Two files under the runtime memory root resolved via the Unified Paths API
(:func:`antigona.core.paths.memory_dir`):
  MEMORY.md  — agent notes (environment, conventions, lessons), limit 2200 chars
  USER.md    — user profile (name, style, preferences), limit 1375 chars

In a hardened deployment the code root is read-only, so the memory root lives
outside the source tree (e.g. ``<ANTIGONA_STATE_ROOT>/.memory``).

Format:
    § TITLE
    content
    § ANOTHER_TITLE
    content

Usage::

    memory = FileMemory()
    memory.add_entry("memory", "Python project", "Uses Python 3.11, pytest, ruff")
    memory.add_entry("user", "User name", "Владелец")
    print(memory.get_content("memory"))      # full text
    memory.remove_entry("memory", "Python project")
    assert not memory.is_full("memory")
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

from antigona.core import paths

# ─── Permissions ─────────────────────────────────────────────────────────────
#
# File memory can hold user-profile data — keep it owner-only.

_DIR_MODE = 0o700
_FILE_MODE = 0o600


def _secure_mkdir(path: Path) -> None:
    """Create *path* (and parents) with owner-only permissions."""
    path.mkdir(parents=True, exist_ok=True, mode=_DIR_MODE)
    try:
        os.chmod(path, _DIR_MODE)
    except OSError:  # pragma: no cover - best effort on exotic filesystems
        pass


def _secure_write(path: Path, text: str) -> None:
    """Write *text* to *path* with owner-only permissions."""
    path.write_text(text, encoding="utf-8")
    try:
        os.chmod(path, _FILE_MODE)
    except OSError:  # pragma: no cover - best effort on exotic filesystems
        pass


# ─── Constants ────────────────────────────────────────────────────────────────


def _module_default_memory_dir() -> Path:
    """Import-time memory root; an explicitly configured runtime root wins.

    Used only as the development/test fallback — :class:`FileMemory`
    re-resolves the effective root lazily (see :func:`_default_memory_dir`).
    """
    override = paths.memory_root_override()
    if override is not None:
        return override
    return paths.project_root() / ".memory"


MEMORY_DIR = _module_default_memory_dir()
MEMORY_FILE = MEMORY_DIR / "MEMORY.md"
USER_FILE = MEMORY_DIR / "USER.md"


def _default_memory_dir() -> Path:
    """Effective default memory root for :class:`FileMemory`.

    Priority:

    1. explicit runtime root (``ANTIGONA_MEMORY_ROOT`` / ``ANTIGONA_MEMORY_DIR``
       / ``ANTIGONA_STATE_ROOT``);
    2. immutable deployment → fail closed (never the read-only code root);
    3. otherwise the module default :data:`MEMORY_DIR` (dev/test checkout).
    """
    override = paths.memory_root_override()
    if override is not None:
        return override
    if paths.is_immutable_deployment():
        return paths.memory_root()  # raises — read-only code root
    return MEMORY_DIR

CHAR_LIMITS: dict[str, int] = {
    "memory": 2200,
    "user": 1375,
}

VALID_STORES = frozenset({"memory", "user"})

_STORE_FILES: dict[str, Path] = {
    "memory": MEMORY_FILE,
    "user": USER_FILE,
}

#: Immutable snapshot of the import-time store files. ``_STORE_FILES`` may be
#: redirected by tests; comparing against this snapshot distinguishes a
#: redirected ``_STORE_FILES`` from a redirected ``MEMORY_DIR``.
_CANONICAL_STORE_FILES: dict[str, Path] = dict(_STORE_FILES)


def _store_path(store: str, base_dir: Path | None = None) -> Path:
    """Get the file path for a store, creating dirs/files if needed.

    *base_dir* defaults to the effective runtime memory root
    (:func:`_default_memory_dir`); callers that operate on a custom directory
    (e.g. :class:`FileMemory` instances) pass their own ``memory_dir``.

    A redirected :data:`_STORE_FILES` entry (test isolation) is honored
    verbatim; otherwise the path is ``base_dir / <name>`` so a configured or
    monkeypatched *base_dir* is always followed.
    """
    if base_dir is None:
        base_dir = _default_memory_dir()
    entry = _STORE_FILES[store]
    if entry != _CANONICAL_STORE_FILES[store]:
        # _STORE_FILES was redirected (e.g. test isolation) — honor it verbatim.
        p = entry
    else:
        # Follow a redirected/configured base dir; a monkeypatched MEMORY_DIR
        # (the dev default) is honored here as well.
        p = base_dir / entry.name
    _secure_mkdir(p.parent)
    if not p.exists():
        _secure_write(p, "")
    return p


def _ensure_stores(base_dir: Path | None = None) -> None:
    """Create files if they don't exist."""
    for store in ("memory", "user"):
        _store_path(store, base_dir)


# ─── Parsing helpers ──────────────────────────────────────────────────────────


def _iter_blocks(text: str) -> Iterator[tuple[str, str]]:
    """Yield (title, content) for each §-delimited block in *text*.

    Format::

        § Title 1
        content line 1
        content line 2
        § Title 2
        more content
    """
    lines = text.split("\n")
    current_title: str | None = None
    current_lines: list[str] = []

    def _flush() -> Iterator[tuple[str, str]]:
        nonlocal current_title, current_lines
        if current_title is not None:
            yield current_title, "\n".join(current_lines).strip()
        current_title = None
        current_lines = []

    for line in lines:
        if line.startswith("§ "):
            yield from _flush()
            current_title = line[2:].strip()
        elif current_title is not None:
            current_lines.append(line)

    yield from _flush()


def _parse_entries(text: str) -> dict[str, str]:
    """Parse ``§ TITLE\\ncontent`` format into {title: content} dict."""
    return {title: content for title, content in _iter_blocks(text)}


def _serialize_entries(entries: dict[str, str]) -> str:
    """Serialize {title: content} dict back to ``§ TITLE\\ncontent`` format."""
    parts: list[str] = []
    for title, content in entries.items():
        parts.append(f"§ {title}")
        if content:
            parts.append(content)
    return "\n".join(parts)


# ─── FileMemory class ─────────────────────────────────────────────────────────


class FileMemory:
    """Two-file memory: MEMORY.md (agent notes) and USER.md (user profile).

    Attributes:
        memory_dir: Path to the runtime memory directory.
    """

    def __init__(self, memory_dir: str | Path | None = None) -> None:
        self.memory_dir = Path(memory_dir) if memory_dir else _default_memory_dir()
        _secure_mkdir(self.memory_dir)
        _ensure_stores(self.memory_dir)

    # ── Public API ──────────────────────────────────────────────────────

    def _path(self, store: str) -> Path:
        """Resolve a store's file under this instance's ``memory_dir``."""
        return _store_path(store, self.memory_dir)

    def add_entry(self, store: str, title: str, content: str) -> None:
        """Add or replace an entry in the given store.

        Args:
            store: ``"memory"`` or ``"user"``.
            title: Entry title (e.g. ``"User name"``, ``"Project info"``).
            content: Entry content text.

        Raises:
            ValueError: If ``store`` is not valid or the entry would overflow.
        """
        self._validate_store(store)
        path = self._path(store)
        entries = _parse_entries(path.read_text(encoding="utf-8"))
        entries[title.strip()] = content.strip()
        serialized = _serialize_entries(entries)
        limit = CHAR_LIMITS[store]
        if len(serialized) > limit:
            raise ValueError(
                f"{store.upper()}.md would exceed {limit} char limit "
                f"({len(serialized)} chars)"
            )
        _secure_write(path, serialized)

    def remove_entry(self, store: str, title: str) -> bool:
        """Remove an entry by title.

        Args:
            store: ``"memory"`` or ``"user"``.
            title: Entry title to remove.

        Returns:
            True if the entry was removed, False if not found.
        """
        self._validate_store(store)
        path = self._path(store)
        entries = _parse_entries(path.read_text(encoding="utf-8"))
        if title.strip() not in entries:
            return False
        del entries[title.strip()]
        _secure_write(path, _serialize_entries(entries))
        return True

    def get_content(self, store: str) -> str:
        """Get full content of a store (raw text).

        Args:
            store: ``"memory"`` or ``"user"``.

        Returns:
            The full file content, or ``""`` if empty.
        """
        self._validate_store(store)
        path = self._path(store)
        return path.read_text(encoding="utf-8").strip()

    def is_full(self, store: str) -> bool:
        """Check if the store has reached or exceeded its char limit.

        Args:
            store: ``"memory"`` or ``"user"``.

        Returns:
            True if length >= limit.
        """
        self._validate_store(store)
        content = self.get_content(store)
        limit = CHAR_LIMITS[store]
        return len(content) >= limit

    def get_entries(self, store: str) -> dict[str, str]:
        """Return all entries as {title: content} dict.

        Args:
            store: ``"memory"`` or ``"user"``.

        Returns:
            Entries dict.
        """
        self._validate_store(store)
        path = self._path(store)
        return _parse_entries(path.read_text(encoding="utf-8"))

    # ── Snapshot (frozen, for system prompt injection) ────────────────────

    def get_snapshot(self) -> dict[str, str]:
        """Return frozen snapshot of both stores.

        Returns::

            {"memory": "§ ...\\n...", "user": "§ ...\\n..."}
        """
        return {
            "memory": self.get_content("memory"),
            "user": self.get_content("user"),
        }

    def has_content(self, store: str) -> bool:
        """Check if a store has any content."""
        return bool(self.get_content(store).strip())

    # ── Helpers ──────────────────────────────────────────────────────────

    def _validate_store(self, store: str) -> None:
        if store not in VALID_STORES:
            raise ValueError(
                f"Invalid store: {store!r}. Must be one of {sorted(VALID_STORES)}"
            )

    def clear_all(self) -> None:
        """Clear both stores (for testing)."""
        for store in ("memory", "user"):
            _secure_write(self._path(store), "")
