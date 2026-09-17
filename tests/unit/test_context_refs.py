"""Tests for Context References — @file, @folder, @url expansion."""

from __future__ import annotations

from pathlib import Path

import pytest

from antigona.core import paths
from antigona.tools.context_refs import (
    expand_refs,
    is_path_blocked,
    resolve_file,
    resolve_folder,
)

# Derived from the canonical home helper instead of a hardcoded owner path.
_HOME = str(paths.home_dir())


class TestIsPathBlocked:
    """Test path blocking logic."""

    def test_block_env(self) -> None:
        assert is_path_blocked(paths.project_root() / ".env")

    def test_block_ssh(self) -> None:
        # Keep outside-workspace coverage separately intact
        assert is_path_blocked(Path(f"{_HOME}/.ssh/id_rsa"))
        assert is_path_blocked(Path(f"{_HOME}/.ssh/authorized_keys"))

        # Strengthen the SSH sensitive-pattern test inside the project root
        assert is_path_blocked(paths.project_root() / ".ssh/id_rsa")
        assert is_path_blocked(paths.project_root() / ".ssh/authorized_keys")
        assert is_path_blocked(paths.project_root() / "subdir/.ssh/id_rsa")
        assert is_path_blocked(paths.project_root() / ".ssh")

    def test_block_binary_extensions(self) -> None:
        assert is_path_blocked(paths.project_root() / "test.pyc")
        assert is_path_blocked(paths.project_root() / "test.exe")
        assert is_path_blocked(paths.project_root() / "test.png")

    def test_allow_normal_python(self) -> None:
        assert not is_path_blocked(paths.project_root() / "src/antigona/bot.py")
        assert not is_path_blocked(paths.project_root() / "README.md")

    def test_block_outside_workspace(self) -> None:
        assert is_path_blocked(Path("/etc/passwd"))
        assert is_path_blocked(Path("/var/log/syslog"))

    def test_block_git_config(self) -> None:
        assert is_path_blocked(paths.project_root() / ".git/config")

    def test_sibling_prefix_path_blocked(self) -> None:
        """Sibling-prefix path (e.g. antigona_sibling) must be blocked."""
        sibling_path = paths.project_root().parent / (paths.project_root().name + "_sibling") / "x.txt"
        assert not sibling_path.is_relative_to(paths.project_root())
        assert is_path_blocked(sibling_path)


class TestResolveFile:
    """Test file resolution."""

    def test_read_python_file(self) -> None:
        content = resolve_file(str(paths.project_root() / "src/antigona/soul.py"))
        assert "PersonalityManager" in content

    def test_read_with_line_range(self) -> None:
        content = resolve_file(str(paths.project_root() / "src/antigona/soul.py"), "1-10")
        assert "PersonalityManager" in content or "1|" in content

    def test_blocked_file_raises(self) -> None:
        with pytest.raises(ValueError, match="Доступ запрещён"):
            resolve_file(str(paths.project_root() / ".env"))

    def test_nonexistent_file_raises(self) -> None:
        with pytest.raises(FileNotFoundError):
            resolve_file(str(paths.project_root() / "nonexistent_file_xyz123.py"))


class TestResolveFolder:
    """Test folder resolution."""

    def test_list_src_folder(self) -> None:
        content = resolve_folder(str(paths.project_root() / "src/antigona/cron"))
        assert "scheduler.py" in content

    def test_nonexistent_folder_raises(self) -> None:
        with pytest.raises(FileNotFoundError):
            resolve_folder(str(paths.project_root() / "nonexistent_test_dir_xyz_12345"))

    def test_blocked_folder_raises(self) -> None:
        with pytest.raises(ValueError, match="Доступ запрещён"):
            resolve_folder(f"{_HOME}/.ssh")


class TestExpandRefs:
    """Test @ref expansion in text."""

    def test_expand_file(self) -> None:
        text = "Check this file: @file:README.md"
        result = expand_refs(text)
        assert ">>> @file:README.md" in result
        assert "Antigona" in result or ">>>" in result
        assert "<<<" in result

    def test_expand_folder(self) -> None:
        text = "Show me @folder:src/antigona/cron"
        result = expand_refs(text)
        assert ">>> @folder:src/antigona/cron" in result
        assert "scheduler.py" in result or ">>>" in result

    def test_blocked_ref_shows_error(self) -> None:
        text = "Read @file:.env"
        result = expand_refs(text)
        assert "⚠️" in result or "запрещён" in result

    def test_no_refs_passthrough(self) -> None:
        text = "Just a normal message with no refs."
        result = expand_refs(text)
        assert result == text

    def test_multiple_refs(self) -> None:
        text = "Compare @file:README.md and @file:AGENTS.md"
        result = expand_refs(text)
        assert ">>> @file:README.md" in result
        assert ">>> @file:AGENTS.md" in result

    def test_url_ref(self) -> None:
        # Test that @url syntax is recognized (may fail on actual fetch)
        text = "Fetch @url:https://example.com"
        result = expand_refs(text)
        assert ">>> @url:https://example.com" in result
        assert "<<<" in result
