"""Document collection — search files by content or metadata.

Functions:
  - collect_by_content: ripgrep-based content search with root boundary
  - collect_by_metadata: name pattern / type / date range filtering
  - collect_results_to_list: human-readable summary of found files
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from antigona.core import paths

_ROOT_BOUNDARY = paths.project_root().resolve()


class RootBoundaryError(ValueError):
    """Raised when a path escapes the root boundary (paths.project_root())."""


@dataclass
class CollectedFile:
    """Representation of a single collected file."""

    path: str
    size_bytes: int = 0
    modified_at: float = 0.0
    matched_lines: list[str] = field(default_factory=list)


def _enforce_boundary(path: str | Path) -> Path:
    """Resolve a path and verify it stays under the root boundary.

    Raises:
        RootBoundaryError: if the resolved path escapes the root boundary.
    """
    resolved = Path(path).resolve()
    if not resolved.is_relative_to(_ROOT_BOUNDARY):
        raise RootBoundaryError(
            f"Path '{path}' resolves to '{resolved}' which is outside {_ROOT_BOUNDARY}"
        )
    return resolved


def _check_paths_inside_boundary(paths: Sequence[str | Path]) -> list[Path]:
    """Resolve multiple paths, raising on boundary escape."""
    result: list[Path] = []
    for p in paths:
        resolved = _enforce_boundary(p)
        result.append(resolved)
    return result


def _collect_by_content_pure_python(
    query: str,
    search_root: Path,
    file_glob: str | None = None,
) -> list[CollectedFile]:
    """Pure-Python fallback for collect_by_content when ripgrep is missing.

    It respects file_glob, is_relative_to project root boundary, skips non-text,
    handles invalid regex safely, and retrieves file size/mtime.
    """
    try:
        pattern = re.compile(query)
    except re.error:
        return []

    collected: list[CollectedFile] = []

    for dirpath, dirnames, filenames in os.walk(str(search_root)):
        # Skip hidden directories
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]

        for filename in filenames:
            if filename.startswith("."):
                continue

            candidate_path = Path(dirpath) / filename

            # Enforce canonical project-root boundary, including symlink/resolved-path escapes
            try:
                resolved_path = candidate_path.resolve()
            except (OSError, RuntimeError):
                continue

            if not resolved_path.is_relative_to(_ROOT_BOUNDARY):
                continue

            # Compute relative path and use relative_path.match(file_glob)
            relative_path = candidate_path.relative_to(search_root)
            if file_glob and not relative_path.match(file_glob):
                continue

            # Skip unreadable/non-text files safely
            try:
                st = resolved_path.stat()
                matched_lines: list[str] = []
                is_binary = False
                with open(resolved_path, encoding="utf-8", errors="strict") as f:
                    for line_content in f:
                        if "\x00" in line_content:
                            is_binary = True
                            break
                        line_stripped = line_content.rstrip("\r\n")
                        if pattern.search(line_stripped):
                            matched_lines.append(line_stripped)
                if is_binary:
                    continue
            except (OSError, UnicodeError):
                continue

            if matched_lines:
                collected.append(
                    CollectedFile(
                        path=str(resolved_path),
                        size_bytes=st.st_size,
                        modified_at=st.st_mtime,
                        matched_lines=matched_lines,
                    )
                )

    return collected


def collect_by_content(
    query: str,
    path: str | Path = _ROOT_BOUNDARY,
    file_glob: str | None = None,
) -> list[CollectedFile]:
    """Search for files containing *query* under *path* using ripgrep.

    Args:
        query: Text pattern to search for (regex).
        path: Root directory to search (must be under the root boundary).
        file_glob: Optional glob filter (e.g. '*.py', '*.md').

    Returns:
        List of CollectedFile with matched lines.

    Raises:
        RootBoundaryError: if resolved path escapes the root boundary.
    """
    search_root = _enforce_boundary(path)
    if not search_root.is_dir():
        return []

    cmd = ["rg", "--line-number", "--no-heading", "--color", "never"]

    if file_glob:
        cmd.extend(["--glob", file_glob])

    cmd.append(query)
    cmd.append(str(search_root))

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        return []
    except FileNotFoundError:
        return _collect_by_content_pure_python(query, search_root, file_glob)

    if result.returncode not in (0, 1):
        # ripgrep returns 2 on error
        return []

    files_map: dict[str, CollectedFile] = {}
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split(":", 2)
        if len(parts) < 2:
            continue
        file_path = parts[0]
        if len(parts) >= 2:
            matched_line = parts[2] if len(parts) > 2 else f"(line {parts[1]})"
        else:
            matched_line = "(match)"

        try:
            resolved_path = Path(file_path).resolve()
        except (OSError, RuntimeError):
            continue
        if not resolved_path.is_relative_to(_ROOT_BOUNDARY):
            continue

        resolved_path_str = str(resolved_path)
        if resolved_path_str not in files_map:
            try:
                st = resolved_path.stat()
            except OSError:
                st = os.stat_result([0] * 10)
            files_map[resolved_path_str] = CollectedFile(
                path=resolved_path_str,
                size_bytes=st.st_size,
                modified_at=st.st_mtime,
                matched_lines=[],
            )
        files_map[resolved_path_str].matched_lines.append(matched_line)

    return list(files_map.values())


def collect_by_metadata(
    name_pattern: str | None = None,
    type_filter: str | None = None,
    date_range: tuple[float, float] | None = None,
    path: str | Path = _ROOT_BOUNDARY,
) -> list[CollectedFile]:
    """Search for files by name pattern, type, or modification date range.

    Args:
        name_pattern: Glob-like pattern for filename (e.g. '*.py', '*config*').
        type_filter: File type filter ('file', 'dir', 'symlink').
        date_range: (start_timestamp, end_timestamp) for mtime filtering.
        path: Root directory to search (must be under the root boundary).

    Returns:
        List of CollectedFile matching the criteria.

    Raises:
        RootBoundaryError: if resolved path escapes the root boundary.
    """
    search_root = _enforce_boundary(path)
    if not search_root.is_dir():
        return []

    # Build regex from glob pattern
    name_re: re.Pattern[str] | None = None
    if name_pattern:
        # Convert simple glob to regex
        regex_str = re.escape(name_pattern).replace(r"\*", ".*").replace(r"\?", ".")
        name_re = re.compile(f"^{regex_str}$")

    result: list[CollectedFile] = []
    try:
        for dirpath, dirnames, filenames in os.walk(str(search_root)):
            # Skip hidden directories
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]

            entries: list[str] = []
            if type_filter in (None, "file", ""):
                entries.extend(filenames)
            if type_filter in (None, "dir", ""):
                entries.extend(dirnames)
            # symlink is separate
            if type_filter == "symlink":
                for name in filenames + dirnames:
                    full = os.path.join(dirpath, name)
                    if os.path.islink(full):
                        entries.append(name)

            for name in entries:
                full_path = os.path.join(dirpath, name)

                # Name pattern filter
                if name_re and not name_re.search(name):
                    continue

                # Date range filter
                if date_range:
                    try:
                        mtime = os.path.getmtime(full_path)
                    except OSError:
                        continue
                    start, end = date_range
                    if mtime < start or mtime > end:
                        continue

                try:
                    st = os.stat(full_path)
                except OSError:
                    st = os.stat_result([0] * 10)

                result.append(
                    CollectedFile(
                        path=full_path,
                        size_bytes=st.st_size,
                        modified_at=st.st_mtime,
                    )
                )

    except OSError:
        pass

    return result


def collect_results_to_list(files: list[CollectedFile]) -> str:
    """Format a list of CollectedFile into a human-readable summary.

    Args:
        files: List of collected files.

    Returns:
        Multi-line string with file info.
    """
    if not files:
        return "No files found."

    lines: list[str] = [f"Found {len(files)} file(s):", ""]
    for f in sorted(files, key=lambda x: x.path):
        size_str = _format_size(f.size_bytes)
        mtime_str = (
            datetime.fromtimestamp(f.modified_at).strftime("%Y-%m-%d %H:%M:%S")
            if f.modified_at
            else "unknown"
        )
        lines.append(f"  {f.path}")
        lines.append(f"    Size: {size_str}  Modified: {mtime_str}")

        if f.matched_lines:
            preview = f.matched_lines[:5]
            lines.append(f"    Matches ({len(f.matched_lines)} total):")
            for ml in preview:
                display = ml[:120]
                lines.append(f"      > {display}")
            if len(f.matched_lines) > 5:
                lines.append(f"      ... and {len(f.matched_lines) - 5} more")
        lines.append("")

    return "\n".join(lines)


def _format_size(bytes_val: int) -> str:
    """Format byte count to human-readable string."""
    if bytes_val < 1024:
        return f"{bytes_val} B"
    elif bytes_val < 1024 * 1024:
        return f"{bytes_val / 1024:.1f} KB"
    elif bytes_val < 1024 * 1024 * 1024:
        return f"{bytes_val / (1024 * 1024):.1f} MB"
    return f"{bytes_val / (1024 * 1024 * 1024):.1f} GB"
