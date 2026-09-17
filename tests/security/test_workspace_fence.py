"""P1-FENCE regression matrix for both workspace path guards."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from antigona.filesystem import WorkspaceViolation, validate_relative_path
from antigona.worker.tools import ToolError, WorkspaceGuard

DENIED_LEXICAL_PATHS = [
    pytest.param("..", id="dotdot"),
    pytest.param("a/../../etc", id="dotdot-nested"),
    pytest.param("/etc/passwd", id="posix-absolute"),
    pytest.param("C:/Windows/System32", id="windows-drive-absolute"),
    pytest.param("C:\\Windows\\System32", id="windows-drive-backslash"),
    pytest.param("\\\\server\\share\\secret", id="windows-unc"),
    pytest.param("a/./b", id="normalize-dot-segment"),
    pytest.param("a//b", id="normalize-empty-segment"),
    pytest.param("a/b/", id="normalize-trailing-separator"),
    pytest.param("%2e%2e%2fsecret", id="percent-traversal"),
    pytest.param("..%2f..%2fsecret", id="percent-separator"),
    pytest.param("%252e%252e%252fsecret", id="double-percent"),
    pytest.param("％2e％2e％2fsecret", id="fullwidth-percent"),
    pytest.param("‥/secret", id="unicode-two-dot-leader"),
    pytest.param("．．／secret", id="unicode-fullwidth-traversal"),
    pytest.param("..\\..\\secret", id="backslash-traversal"),
]

SAFE_PATHS = [
    pytest.param("a/b/c.txt", id="plain"),
    pytest.param("safe/Ａ.txt", id="fullwidth-letter"),
    pytest.param("file with spaces.txt", id="spaces"),
    pytest.param("файл.txt", id="cyrillic"),
    pytest.param("100%.txt", id="literal-percent"),
    pytest.param("report．txt", id="compatibility-dot-filename"),
]


def _filesystem_guard(workspace: Path, path: str) -> Path:
    validate_relative_path(workspace, path)
    return workspace / path


def _worker_guard(workspace: Path, path: str) -> Path:
    return WorkspaceGuard(workspace).resolve(path)


GUARDS: list[tuple[str, type[ValueError], Callable[[Path, str], Path]]] = [
    ("filesystem", WorkspaceViolation, _filesystem_guard),
    ("worker", ToolError, _worker_guard),
]


@pytest.mark.parametrize("path", DENIED_LEXICAL_PATHS)
@pytest.mark.parametrize(("guard_name", "error", "guard"), GUARDS, ids=[row[0] for row in GUARDS])
def test_guards_deny_ambiguous_or_escaping_lexical_paths(
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


@pytest.mark.parametrize("path", SAFE_PATHS)
@pytest.mark.parametrize(("guard_name", "error", "guard"), GUARDS, ids=[row[0] for row in GUARDS])
def test_guards_allow_unambiguous_relative_paths(
    tmp_path: Path,
    path: str,
    guard_name: str,
    error: type[ValueError],
    guard: Callable[[Path, str], Path],
) -> None:
    del guard_name, error
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    guard(workspace, path)


@pytest.mark.parametrize(("guard_name", "error", "guard"), GUARDS, ids=[row[0] for row in GUARDS])
@pytest.mark.parametrize("shape", ["intermediate", "final", "chain"])
def test_guards_deny_every_existing_symlink_component(
    tmp_path: Path,
    guard_name: str,
    error: type[ValueError],
    guard: Callable[[Path, str], Path],
    shape: str,
) -> None:
    del guard_name
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    if shape == "intermediate":
        (workspace / "link").symlink_to(outside, target_is_directory=True)
        path = "link/secret"
    elif shape == "final":
        target = workspace / "target.txt"
        target.write_text("safe", encoding="utf-8")
        (workspace / "link.txt").symlink_to(target)
        path = "link.txt"
    else:
        (workspace / "second").symlink_to(outside, target_is_directory=True)
        (workspace / "first").symlink_to(workspace / "second", target_is_directory=True)
        path = "first/secret"

    with pytest.raises(error):
        guard(workspace, path)
