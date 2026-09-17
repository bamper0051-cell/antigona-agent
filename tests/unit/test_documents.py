"""Tests for the Document Collector (antigona/tools/documents.py).

Covers:
  - collect_by_content: search, boundary, empty result
  - collect_by_metadata: name/type/date filters
  - collect_results_to_list: formatting
  - Root boundary enforcement
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from antigona.core import paths
from antigona.tools.documents import (
    RootBoundaryError,
    _enforce_boundary,
    collect_by_content,
    collect_by_metadata,
    collect_results_to_list,
)

# Derived from the canonical home helper instead of a hardcoded owner path.
_HOME = str(paths.home_dir())


@pytest.fixture
def force_fallback(monkeypatch):
    """Force the use of the pure-Python fallback by mocking subprocess.run."""
    def mock_run(*args, **kwargs):
        raise FileNotFoundError("Mocked: rg not found")
    monkeypatch.setattr(subprocess, "run", mock_run)


@pytest.fixture
def temp_project(monkeypatch, tmp_path):
    """Set up a temporary project root boundary."""
    boundary = tmp_path / "project"
    boundary.mkdir()
    import antigona.tools.documents as docs
    monkeypatch.setattr(docs, "_ROOT_BOUNDARY", boundary.resolve())
    return boundary


class TestRootBoundary:
    """Tests for root boundary enforcement (paths.project_root())."""

    def test_inside_boundary(self) -> None:
        """Path inside the root boundary should resolve without error."""
        resolved = _enforce_boundary(paths.project_root() / "some/file.txt")
        assert resolved.is_relative_to(paths.project_root())

    def test_boundary_exact_root(self) -> None:
        """Exact boundary path should be allowed."""
        resolved = _enforce_boundary(paths.project_root())
        assert resolved == paths.project_root()

    def test_outside_boundary_raises(self) -> None:
        """Path escaping the project root should raise RootBoundaryError."""
        with pytest.raises(RootBoundaryError):
            _enforce_boundary("/etc/passwd")

    def test_outside_boundary_traversal(self) -> None:
        """Relative traversal escaping the project root should raise."""
        with pytest.raises(RootBoundaryError):
            _enforce_boundary(f"{_HOME}/../../etc")

    def test_var_path_outside(self) -> None:
        """/var/tmp is outside the project root."""
        with pytest.raises(RootBoundaryError):
            _enforce_boundary("/var/tmp")

    def test_sibling_prefix_boundary_raises(self) -> None:
        """Sibling-prefix path (e.g. antigona_sibling) must be blocked, despite sharing prefix."""
        sibling_path = paths.project_root().parent / (paths.project_root().name + "_sibling") / "x.txt"
        assert not sibling_path.is_relative_to(paths.project_root())
        with pytest.raises(RootBoundaryError):
            _enforce_boundary(sibling_path)


class TestCollectByContent:
    """Tests for collect_by_content (ripgrep-based search)."""

    def test_search_finds_matches(self) -> None:
        """Search for an existing pattern should return matches."""
        files = collect_by_content("import", path=str(paths.project_root() / "src"), file_glob="*.py")
        assert len(files) > 0
        # All returned files should be under the root boundary
        for f in files:
            assert Path(f.path).is_relative_to(paths.project_root())

    def test_search_empty_result(self) -> None:
        """Search for a non-existent pattern should return empty list."""
        files = collect_by_content(
            "XYZZYX_NONEXISTENT_PATTERN_12345",
            path=str(paths.project_root() / "src"),
            file_glob="*.py",
        )
        assert len(files) == 0

    def test_search_glob_filter(self) -> None:
        """Glob filter should narrow results."""
        py_files = collect_by_content("def", path=str(paths.project_root() / "src"), file_glob="*.py")
        md_files = collect_by_content("def", path=str(paths.project_root() / "src"), file_glob="*.md")
        # .md files shouldn't contain Python function definitions
        assert len(md_files) <= len(py_files)

    def test_search_nonexistent_directory(self) -> None:
        """Search in a non-existent directory returns empty."""
        files = collect_by_content("test", path=str(paths.project_root() / "nonexistent_dir_12345"))
        assert len(files) == 0

    def test_search_empty_query(self) -> None:
        """Empty query should be treated as a regex (match everything) or empty."""
        # ripgrep with empty string just returns nothing
        files = collect_by_content("", path=str(paths.project_root() / "src"), file_glob="*.py")
        assert isinstance(files, list)


class TestCollectByMetadata:
    """Tests for collect_by_metadata."""

    def test_by_name_pattern(self) -> None:
        """Search by name pattern should return matching files."""
        files = collect_by_metadata(name_pattern="*.py", path=str(paths.project_root() / "src"))
        assert len(files) > 0
        for f in files:
            assert f.path.endswith(".py")

    def test_by_type_file(self) -> None:
        """Filter by type='file' should return only files."""
        files = collect_by_metadata(type_filter="file", path=str(paths.project_root() / "src"))
        assert len(files) > 0

    def test_by_date_range(self) -> None:
        """Filter by date range should work with valid timestamps."""
        import time

        now = time.time()
        recent = collect_by_metadata(
            date_range=(now - 86400 * 30, now),
            path=str(paths.project_root() / "src"),
        )
        # At least some files should exist
        assert isinstance(recent, list)

    def test_empty_result(self) -> None:
        """Non-matching pattern returns empty list."""
        files = collect_by_metadata(
            name_pattern="XYZZYX_NONEXISTENT_98765",
            path=str(paths.project_root() / "src"),
        )
        assert len(files) == 0

    def test_outside_boundary(self) -> None:
        """Path outside /opt/antigona-home should raise."""
        with pytest.raises(RootBoundaryError):
            collect_by_metadata(path="/tmp")


class TestCollectResultsToList:
    """Tests for collect_results_to_list formatting."""

    def test_empty_list(self) -> None:
        """Empty list returns 'No files found.'"""
        result = collect_results_to_list([])
        assert "No files found." in result

    def test_contains_file_paths(self) -> None:
        """File paths should appear in the output."""
        from antigona.tools.documents import CollectedFile

        files = [
            CollectedFile(path=f"{_HOME}/test.py", size_bytes=1024, modified_at=0),
        ]
        result = collect_results_to_list(files)
        assert "test.py" in result
        assert "1.0 KB" in result or "1024" in result

    def test_multiple_files(self) -> None:
        """Multiple files should all appear."""
        from antigona.tools.documents import CollectedFile

        files = [
            CollectedFile(path=f"{_HOME}/a.py", size_bytes=100),
            CollectedFile(path=f"{_HOME}/b.py", size_bytes=200),
        ]
        result = collect_results_to_list(files)
        assert "a.py" in result
        assert "b.py" in result
        assert "2 file(s)" in result


class TestPurePythonFallback:
    """Focused tests for the pure-Python fallback mechanism."""

    def test_positive_and_glob_exclusion(self, force_fallback, temp_project) -> None:
        """Test that matches are found and glob filters exclude non-matching files."""
        src = temp_project / "src"
        src.mkdir()

        match_py = src / "match.py"
        match_py.write_text("def test_func():\n    pass\n", encoding="utf-8")

        no_match_py = src / "no_match.py"
        no_match_py.write_text("class Another:\n    pass\n", encoding="utf-8")

        match_txt = src / "match.txt"
        match_txt.write_text("def test_func():\n    pass\n", encoding="utf-8")

        # Glob *.py should find match_py, but exclude match_txt and no_match_py
        results = collect_by_content("test_func", path=src, file_glob="*.py")
        assert len(results) == 1
        assert results[0].path == str(match_py.resolve())
        assert results[0].matched_lines == ["def test_func():"]

        st = match_py.resolve().stat()
        assert results[0].size_bytes == st.st_size
        assert results[0].modified_at == st.st_mtime

    def test_invalid_regex(self, force_fallback, temp_project) -> None:
        """Test that invalid regex pattern returns an empty list immediately."""
        src = temp_project / "src"
        src.mkdir()
        match_py = src / "match.py"
        match_py.write_text("def test_func():\n    pass\n", encoding="utf-8")

        results = collect_by_content("[invalid_re", path=src, file_glob="*.py")
        assert results == []

    def test_binary_skip(self, force_fallback, temp_project) -> None:
        """Test that files containing null bytes (binary) are skipped without error."""
        src = temp_project / "src"
        src.mkdir()
        binary_file = src / "binary.py"
        binary_file.write_bytes(b"def test_func():\x00\n")

        results = collect_by_content("test_func", path=src, file_glob="*.py")
        assert results == []

    def test_symlink_escape(self, force_fallback, temp_project) -> None:
        """Test that a symlink pointing outside the project boundary is skipped."""
        outside_dir = temp_project.parent / "outside"
        outside_dir.mkdir()
        outside_file = outside_dir / "match.py"
        outside_file.write_text("def test_func():\n    pass\n", encoding="utf-8")

        src = temp_project / "src"
        src.mkdir()
        symlink_file = src / "escape.py"
        symlink_file.symlink_to(outside_file)

        results = collect_by_content("test_func", path=src, file_glob="*.py")
        assert results == []
