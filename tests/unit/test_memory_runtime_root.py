"""Regression: file-memory runtime root must live outside an immutable code root.

Covers the Phase C.1 boot blocker where ``FileMemory()`` created ``.memory``
under the read-only code root (``OSError: [Errno 30] Read-only file system``).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from antigona.context.builder import ContextBuilder
from antigona.core import paths
from antigona.memory import file_memory
from antigona.memory.file_memory import FileMemory

_MEMORY_ENV = (
    "ANTIGONA_MEMORY_ROOT",
    "ANTIGONA_MEMORY_DIR",
    "ANTIGONA_STATE_ROOT",
    "ANTIGONA_IMMUTABLE_DEPLOYMENT",
)


@pytest.fixture(autouse=True)
def _clean_memory_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate every test from ambient runtime-root configuration."""
    for name in _MEMORY_ENV:
        monkeypatch.delenv(name, raising=False)


def test_state_root_routes_memory_and_sets_private_perms(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state_root = tmp_path / "var-lib-antigona"
    source_memory = tmp_path / "immutable-source" / ".memory"
    monkeypatch.setenv("ANTIGONA_STATE_ROOT", str(state_root))
    monkeypatch.setattr(file_memory, "MEMORY_DIR", source_memory)

    memory = FileMemory()

    assert memory.memory_dir == state_root / ".memory"
    assert (memory.memory_dir / "MEMORY.md").is_file()
    assert (memory.memory_dir / "USER.md").is_file()
    assert memory.memory_dir.stat().st_mode & 0o777 == 0o700
    assert (memory.memory_dir / "MEMORY.md").stat().st_mode & 0o777 == 0o600
    assert not source_memory.exists()


def test_dedicated_memory_root_env_wins(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    dedicated = tmp_path / "dedicated-memory"
    monkeypatch.setenv("ANTIGONA_MEMORY_ROOT", str(dedicated))
    monkeypatch.setenv("ANTIGONA_STATE_ROOT", str(tmp_path / "state"))

    assert paths.memory_dir() == dedicated.resolve()
    assert paths.learnings_file() == dedicated.resolve() / "learnings.json"
    assert FileMemory().memory_dir == dedicated.resolve()


def test_gateway_startup_with_read_only_code_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A read-only code root plus a configured runtime root must not raise."""
    state_root = tmp_path / "runtime-state"
    code_memory = tmp_path / "immutable-code" / ".memory"
    monkeypatch.setenv("ANTIGONA_IMMUTABLE_DEPLOYMENT", "1")
    monkeypatch.setenv("ANTIGONA_STATE_ROOT", str(state_root))
    monkeypatch.setattr(file_memory, "MEMORY_DIR", code_memory)

    memory = FileMemory()
    memory.add_entry("memory", "boot", "gateway started")

    assert "gateway started" in memory.get_content("memory")
    assert memory.memory_dir == state_root / ".memory"
    assert not code_memory.exists()


def test_context_builder_default_uses_runtime_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The gateway's memory consumer (ContextBuilder) honors the runtime root."""
    state_root = tmp_path / "runtime-state"
    monkeypatch.setenv("ANTIGONA_IMMUTABLE_DEPLOYMENT", "1")
    monkeypatch.setenv("ANTIGONA_STATE_ROOT", str(state_root))

    builder = ContextBuilder()

    assert builder.file_memory is not None
    assert builder.file_memory.memory_dir == state_root / ".memory"


def test_immutable_deployment_refuses_code_root_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    code_memory = tmp_path / "immutable-code" / ".memory"
    monkeypatch.setenv("ANTIGONA_IMMUTABLE_DEPLOYMENT", "1")
    monkeypatch.setattr(file_memory, "MEMORY_DIR", code_memory)

    with pytest.raises(RuntimeError, match="ANTIGONA_STATE_ROOT"):
        FileMemory()
    # fail closed: the code-root fallback is never written
    assert not code_memory.exists()


def _path_signature(path: Path) -> tuple[int, int, int] | None:
    """Identity of *path* itself: ``(inode, size, mtime_ns)`` or ``None``.

    Cheap and exception-safe: an unreadable / missing path is reported as
    ``None`` instead of raising, so the snapshot can be taken against an
    ambient checkout whose state we do not control.
    """
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_ino, st.st_size, st.st_mtime_ns)


_TREE_WALK_LIMIT = 2000


def _tree_signature(path: Path) -> dict[str, tuple[int, int, int] | None]:
    """Recursive signature of the tree under *path* (B46c: was direct children).

    Returns a mapping ``relative/posix/path -> (inode, size, mtime_ns)``; an
    entry that cannot be ``stat``-ed maps to ``None``, and a directory that
    does not exist (or cannot be listed) contributes nothing.  The walk is
    breadth-first, never follows symlinks and is bounded at
    ``_TREE_WALK_LIMIT`` entries; if the bound is reached a
    ``<walk truncated at N entries>`` key is added so a difference against the
    pre-write snapshot is still detected and the bound is never silent.
    """
    signature: dict[str, tuple[int, int, int] | None] = {}
    queue: list[Path] = [path]
    while queue:
        current = queue.pop(0)
        try:
            children = sorted(current.iterdir())
        except OSError:
            continue
        for child in children:
            if len(signature) >= _TREE_WALK_LIMIT - 1:
                signature[f"<walk truncated at {_TREE_WALK_LIMIT} entries>"] = None
                return signature
            try:
                key = child.relative_to(path).as_posix()
            except ValueError:  # pragma: no cover - defensive, same root
                key = child.name
            signature[key] = _path_signature(child)
            try:
                if child.is_dir() and not child.is_symlink():
                    queue.append(child)
            except OSError:
                continue
    return signature


def test_no_memory_dir_created_under_source_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A configured runtime root means the code root is never written to.

    Hermetic: the checkout may legitimately contain a *pre-existing* leftover
    ``<code root>/.memory`` (created by an out-of-suite CLI/agent run — see the
    session-scoped guard in ``tests/conftest.py``).  Such a leftover must not
    decide this test's verdict, so the code-root path state (existence, the
    full recursive tree with each entry's inode/size/mtime — see defect B46c)
    is snapshotted before the call and compared after it.  A *new* creation or
    write by the product still fails the test, including a write into a nested
    subdirectory of a pre-existing ``.memory``.
    """
    monkeypatch.setenv("ANTIGONA_STATE_ROOT", str(tmp_path / "runtime-state"))
    code_memory = file_memory.MEMORY_DIR

    existed_before = code_memory.exists()
    self_before = _path_signature(code_memory)
    children_before = _tree_signature(code_memory)

    memory = FileMemory()
    memory.add_entry("user", "name", "owner")
    memory.add_entry("memory", "note", "x")

    # Regression intent: the configured runtime root wins over the code root.
    assert memory.memory_dir != code_memory
    assert memory.memory_dir.is_relative_to(tmp_path)

    # Hermetic verdict: the product changed nothing under the code root.
    if not existed_before:
        assert not code_memory.exists(), (
            f"FileMemory() created {code_memory} under the code root even "
            "though ANTIGONA_STATE_ROOT was configured"
        )
    assert _path_signature(code_memory) == self_before, (
        f"FileMemory() modified {code_memory} under the code root: "
        f"{self_before!r} -> {_path_signature(code_memory)!r}"
    )
    assert _tree_signature(code_memory) == children_before, (
        f"FileMemory() wrote into {code_memory} under the code root: "
        f"{sorted(children_before)} -> {sorted(_tree_signature(code_memory))}"
    )



def test_paths_memory_dir_dev_default_is_project_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    dev_root = tmp_path / "dev-checkout"
    dev_root.mkdir()
    monkeypatch.setattr(paths, "project_root", lambda: dev_root)

    assert paths.memory_dir() == dev_root / ".memory"


def test_non_production_default_behavior_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With no runtime-root configuration the dev default stays source-local."""
    dev_memory = tmp_path / "dev-source" / ".memory"
    monkeypatch.setattr(file_memory, "MEMORY_DIR", dev_memory)

    memory = FileMemory()

    assert memory.memory_dir == dev_memory
    assert (dev_memory / "MEMORY.md").is_file()
    assert (dev_memory / "USER.md").is_file()
